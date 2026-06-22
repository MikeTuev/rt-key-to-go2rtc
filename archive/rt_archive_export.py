#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
rt_archive_export.py

Export archived video from a key.rt.ru / camera.rt.ru camera.

The camera.rt.ru web player streams fragmented-MP4 (fMP4) over a WebSocket.
For archive playback the client opens the same WS endpoint used for live and
sends a few text commands:

    seek: <unix_ts>     # jump to a moment in the archive
    speed: <N>          # playback / pull speed multiplier (1,2,4,8,16,32...)
    resume              # start sending media

The server answers with text control frames and binary media frames:

    TEXT  source: "storage"                       # playing from archive
    TEXT  speed: 16.00                            # confirmed speed
    TEXT  seeked                                  # seek applied, flush done
    BIN   ftyp...moov                             # init segment (once)
    TEXT  pts: {"begin": <unix>, "end": <unix>}   # timing of next fragment
    BIN   moof...mdat                             # one ~0.5s fragment
    ...

This tool authenticates with an access token (the long-lived oauth2.key.rt.ru
JWT), looks up the camera to obtain a fresh per-camera "streamer_token" and the
streaming host, then pulls the requested time range and writes a playable MP4.

Key behaviours:
  * Capture is gated on the `seeked` confirmation, so the brief burst of live
    frames the server may send before the seek takes effect is discarded.
  * The server throttles (it never drops fragments) so a high --speed only makes
    the pull faster; pts stays contiguous. ~17x realtime is the practical ceiling.
  * For long exports the streamer_token can expire mid-pull, and the connection
    may drop. The tool reconnects with a fresh token, re-seeking to where it left
    off, writing each session to a separate part file, then stitches the parts
    with ffmpeg at the end.

Examples:
  # 1 hour of archive starting at a unix timestamp, into one mp4
  python3 archive/rt_archive_export.py \\
      --access-token "$ACCESS_TOKEN" \\
      --camera <CAMERA_ID> \\
      --start 1781883685 --duration 1h --out archive/out.mp4

  # Same, using camera-local wall-clock time and an explicit end
  python3 archive/rt_archive_export.py --access-token "$ACCESS_TOKEN" \\
      --camera <CAMERA_ID> \\
      --start "2026-06-19 20:41:25" --end "2026-06-19 21:41:25"

  # Just probe the connection and print what the server sends
  python3 archive/rt_archive_export.py --access-token "$ACCESS_TOKEN" \\
      --camera <CAMERA_ID> --start 1781883685 --probe
"""

import argparse
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

try:
    import websockets
except ImportError:
    print("Missing dependency: pip install websockets", file=sys.stderr)
    raise

CAMERAS_URL = "https://vc.key.rt.ru/api/v1/cameras?limit=100&offset=0"

# Stream query params, matching the working web-player URL.
FRAGMENT_LENGTH_DEFAULT = 0.5
DEFAULT_SPEED = 16

# How long to wait without any media before treating the session as stalled.
STALL_TIMEOUT = 20.0
# How long to wait for the `seeked` confirmation after sending commands.
SEEK_TIMEOUT = 25.0
# Max consecutive reconnect attempts that make no forward progress.
MAX_DEAD_RECONNECTS = 4


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def jwt_exp(token: str):
    """Return the `exp` (unix seconds) of a JWT, or None if not parseable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


def parse_duration(text: str) -> float:
    """Parse '90', '90s', '15m', '2h', '1h30m', '1d' -> seconds (float)."""
    text = str(text).strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    total = 0.0
    matched = False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)([dhms])", text):
        total += float(value) * units[unit]
        matched = True
    if not matched:
        raise ValueError(f"Cannot parse duration: {text!r}")
    return total


def parse_time(text: str, utc_offset_min: int) -> float:
    """
    Parse a start/end time into a unix timestamp (float).

    Accepts:
      * a unix timestamp (int/float), e.g. 1781883685
      * 'now'
      * an ISO-ish local wall-clock string interpreted in the camera's
        timezone (utc_offset_min), e.g. '2026-06-19 20:41:25'
    """
    text = str(text).strip()
    if text.lower() == "now":
        return time.time()
    if re.fullmatch(r"\d{9,11}(\.\d+)?", text):
        return float(text)

    tz = timezone(timedelta(minutes=utc_offset_min))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=tz)
            return dt.timestamp()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse time: {text!r}")


def fmt_local(ts: float, utc_offset_min: int) -> str:
    tz = timezone(timedelta(minutes=utc_offset_min))
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S")


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024:
            return f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}PB"


# --------------------------------------------------------------------------- #
# Camera lookup
# --------------------------------------------------------------------------- #
def get_camera_info(access_token: str, camera_id: str, timeout: float = 20.0) -> dict:
    """Fetch the camera record and return the fields we need to stream."""
    resp = requests.get(
        CAMERAS_URL,
        headers={"authorization": f"Bearer {access_token}", "accept": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    items = (resp.json().get("data") or {}).get("items") or []
    for item in items:
        if str(item.get("id")) == camera_id:
            streamer_url = item.get("streamer_url") or ""
            host = urlparse(streamer_url).netloc or "live-vdk4.camera.rt.ru"
            return {
                "id": camera_id,
                "title": item.get("title"),
                "streamer_token": item.get("streamer_token"),
                "host": host,
                "utc_offset": int(item.get("utc_offset") or 0),
                "archive_length": int(item.get("archive_length") or 0),  # minutes
            }
    raise RuntimeError(
        f"Camera {camera_id} not found among {len(items)} cameras for this account"
    )


def build_ws_url(host: str, camera_id: str, token: str, fragment_length: float) -> str:
    return (
        f"wss://{host}/stream/{camera_id}/live.mp4"
        f"?mp4-fragment-length={fragment_length}"
        f"&mp4-use-speed=0&mp4-afiller=1&token={token}"
    )


# --------------------------------------------------------------------------- #
# Probe mode
# --------------------------------------------------------------------------- #
async def probe(url: str, seek_ts: float, speed: int, utc_offset: int) -> None:
    print(f"Connecting (probe)...\n  {url[:80]}...")
    async with websockets.connect(url, max_size=None, ping_interval=None) as ws:
        await ws.send(f"seek: {int(seek_ts)}")
        await ws.send(f"speed: {speed}")
        await ws.send("resume")
        t0 = time.time()
        seq = 0
        while time.time() - t0 < 12 and seq < 30:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=8)
            except asyncio.TimeoutError:
                print("  (recv timeout)")
                break
            seq += 1
            if isinstance(msg, (bytes, bytearray)):
                box = bytes(msg[4:8]).decode("latin1", "replace")
                tag = "INIT" if (b"ftyp" in msg[:16] or b"moov" in msg[:64]) else "FRAG"
                print(f"  {seq:>2} BIN  {box:<5} {len(msg):>8} bytes  {tag}")
            else:
                line = msg
                if msg.startswith("pts:"):
                    try:
                        d = json.loads(msg[4:])
                        line = (f'pts begin={fmt_local(d["begin"], utc_offset)} '
                                f'end={fmt_local(d["end"], utc_offset)}')
                    except Exception:
                        pass
                print(f"  {seq:>2} TEXT {line}")


# --------------------------------------------------------------------------- #
# One download session -> one part file
# --------------------------------------------------------------------------- #
async def download_session(url: str, seek_ts: float, end_ts: float, speed: int,
                           part_path: Path, utc_offset: int):
    """
    Open one WebSocket session, seek, and write the archive [seek_ts, end_ts)
    range into `part_path` as a self-contained fMP4 (init + fragments).

    Returns (last_pts_end, bytes_written, reason). `last_pts_end` is None if no
    archive media was captured. `reason` is one of:
      'done'   - reached end_ts
      'stall'  - no media for STALL_TIMEOUT
      'closed' - server closed the connection
      'error'  - exception
    """
    seeked = False
    init_written = False
    pending_pts = None
    last_pts_end = None
    bytes_written = 0
    frags = 0
    reason = "error"
    last_progress = 0.0

    try:
        async with websockets.connect(
            url, max_size=None, ping_interval=None, close_timeout=5
        ) as ws:
            await ws.send(f"seek: {int(seek_ts)}")
            await ws.send(f"speed: {speed}")
            await ws.send("resume")

            with open(part_path, "wb") as out:
                deadline_started = time.time()
                while True:
                    timeout = SEEK_TIMEOUT if not seeked else STALL_TIMEOUT
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    except asyncio.TimeoutError:
                        reason = "stall" if seeked else "error"
                        if not seeked:
                            print("  ! no 'seeked' confirmation within "
                                  f"{SEEK_TIMEOUT:.0f}s", file=sys.stderr)
                        break

                    # --- text control frames ---
                    if isinstance(msg, str):
                        if msg.startswith("pts:"):
                            if not seeked:
                                continue
                            try:
                                pending_pts = json.loads(msg[4:])
                            except Exception:
                                pending_pts = None
                        elif msg == "seeked":
                            seeked = True
                            init_written = False  # next BIN is the init segment
                        elif msg.startswith("source:") or msg.startswith("speed:") \
                                or msg == "reset":
                            pass  # informational
                        continue

                    # --- binary frames ---
                    is_init = b"ftyp" in msg[:16] or b"moov" in msg[:64]
                    if is_init:
                        # The init segment (ftyp+moov) is codec configuration,
                        # identical for live and archive. The server sends it
                        # once at the start of the connection, which may be
                        # during the brief live burst that precedes `seeked`.
                        # Capture the first one we see so the part is playable.
                        if not init_written:
                            out.write(msg)
                            bytes_written += len(msg)
                            init_written = True
                        continue

                    if not seeked:
                        # live/pre-seek media -> discard, wait for the seek
                        continue
                    if not init_written:
                        # never emit media before an init segment
                        continue

                    # media fragment (moof+mdat)
                    out.write(msg)
                    bytes_written += len(msg)
                    frags += 1
                    if pending_pts:
                        last_pts_end = pending_pts.get("end", last_pts_end)
                        pending_pts = None

                    # progress line ~ every 2s of wall clock
                    now = time.time()
                    if now - last_progress >= 2.0 and last_pts_end:
                        captured = last_pts_end - seek_ts
                        wall = now - deadline_started
                        rate = (captured / wall) if wall > 0 else 0
                        pct = ""
                        span = end_ts - seek_ts
                        if span > 0:
                            pct = f" {min(100, captured / span * 100):5.1f}%"
                        sys.stdout.write(
                            f"\r  {fmt_local(last_pts_end, utc_offset)}"
                            f"{pct}  {human_size(bytes_written)}  "
                            f"{rate:4.1f}x  {frags} frags   "
                        )
                        sys.stdout.flush()
                        last_progress = now

                    if last_pts_end and last_pts_end >= end_ts:
                        reason = "done"
                        break
                else:
                    reason = "closed"
    except (websockets.ConnectionClosed, OSError) as exc:
        reason = "closed"
        print(f"\n  ! connection closed: {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        reason = "error"
        print(f"\n  ! session error: {exc}", file=sys.stderr)

    if last_progress:
        sys.stdout.write("\n")
        sys.stdout.flush()
    return last_pts_end, bytes_written, reason


# --------------------------------------------------------------------------- #
# ffmpeg remux / concat
# --------------------------------------------------------------------------- #
def have_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def finalize(parts: list[Path], out_path: Path, faststart: bool) -> None:
    """Combine part files into the final output."""
    parts = [p for p in parts if p.exists() and p.stat().st_size > 0]
    if not parts:
        raise RuntimeError("No data captured; nothing to write")

    # A single session is already a complete, seekable fMP4 (its moov sits at
    # the front), so by default we just MOVE it into place. This avoids writing
    # a second full-size copy of the file, which for a multi-GB export can fill
    # the disk (the faststart remux needs ~2x the space). Use --faststart only
    # if you specifically need a flat (non-fragmented) moov.
    if len(parts) == 1 and not faststart:
        parts[0].replace(out_path)
        print(f"Saved: {out_path}")
        return

    if not have_ffmpeg():
        if len(parts) == 1:
            parts[0].replace(out_path)
            print(f"ffmpeg not found; saved fMP4 as-is: {out_path}")
            return
        raise RuntimeError("ffmpeg not found and multiple parts must be stitched")

    print(f"Remuxing {len(parts)} part(s) -> {out_path} ...")
    tmp_files: list[Path] = []

    if len(parts) == 1:
        cmd = ["ffmpeg", "-y", "-i", str(parts[0]),
               "-c", "copy", "-movflags", "+faststart", str(out_path)]
    else:
        # Reconnect boundaries overlap by a few seconds (re-seek snaps to a
        # keyframe before the cut), which leaves duplicate/decreasing DTS that a
        # plain mp4 concat preserves. Routing each part through MPEG-TS and
        # concatenating with the TS demuxer makes ffmpeg regenerate continuous,
        # monotonic timestamps so the result plays smoothly end to end.
        for i, p in enumerate(parts):
            ts = out_path.with_suffix(f".p{i:03d}.ts")
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", str(p), "-c", "copy",
                 "-bsf:v", "h264_mp4toannexb", "-f", "mpegts", str(ts)],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                print(r.stderr[-2000:], file=sys.stderr)
                raise RuntimeError(f"ffmpeg failed converting part {i} to TS")
            tmp_files.append(ts)
        concat = "concat:" + "|".join(str(t) for t in tmp_files)
        cmd = ["ffmpeg", "-y", "-i", concat, "-c", "copy",
               "-fflags", "+genpts", "-movflags", "+faststart", str(out_path)]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr[-2000:], file=sys.stderr)
        raise RuntimeError("ffmpeg remux failed")

    # cleanup
    for t in tmp_files:
        t.unlink(missing_ok=True)
    for p in parts:
        p.unlink(missing_ok=True)
    print(f"Saved: {out_path}")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
async def run_export(args) -> int:
    info = get_camera_info(args.access_token, args.camera, timeout=args.timeout)
    utc_offset = info["utc_offset"]
    print(f"Camera: {info['title']} ({info['id']})")
    print(f"  host={info['host']}  tz=UTC{utc_offset/60:+.0f}  "
          f"archive={info['archive_length']/60/24:.1f} days")

    start_ts = parse_time(args.start, utc_offset)
    if args.end:
        end_ts = parse_time(args.end, utc_offset)
    else:
        end_ts = start_ts + parse_duration(args.duration)
    if end_ts <= start_ts:
        raise RuntimeError("end must be after start")

    now = time.time()
    if end_ts > now:
        print(f"  ! end is in the future; capping at now ({fmt_local(now, utc_offset)})")
        end_ts = now
    oldest = now - info["archive_length"] * 60
    if start_ts < oldest:
        print(f"  ! start is older than the {info['archive_length']/60/24:.1f}-day "
              f"archive window; oldest available ~ {fmt_local(oldest, utc_offset)}")

    print(f"  range: {fmt_local(start_ts, utc_offset)} .. "
          f"{fmt_local(end_ts, utc_offset)}  "
          f"({timedelta(seconds=int(end_ts - start_ts))})  speed={args.speed}x")

    # probe mode
    if args.probe:
        token = info["streamer_token"]
        url = build_ws_url(info["host"], args.camera, token, args.fragment_length)
        await probe(url, start_ts, args.speed, utc_offset)
        return 0

    out_path = Path(args.out) if args.out else Path(
        f"archive/{args.camera}_{int(start_ts)}.mp4"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    parts: list[Path] = []
    cursor = start_ts
    dead = 0
    part_idx = 0

    try:
        while cursor < end_ts - 1.0:
            # Always fetch a fresh streamer_token; it is short-lived and a stale
            # one is the most likely reason a session dropped.
            token = info["streamer_token"]
            exp = jwt_exp(token)
            if exp is None or exp - time.time() < 120:
                info = get_camera_info(args.access_token, args.camera, timeout=args.timeout)
                token = info["streamer_token"]
            url = build_ws_url(info["host"], args.camera, token, args.fragment_length)

            part_path = out_path.with_suffix(f".part{part_idx:03d}.mp4")
            print(f"\n[session {part_idx}] seek {fmt_local(cursor, utc_offset)}")
            last_end, nbytes, reason = await download_session(
                url, cursor, end_ts, args.speed, part_path, utc_offset
            )

            if last_end and nbytes > 0:
                parts.append(part_path)
                part_idx += 1
                if last_end > cursor + 0.5:
                    cursor = last_end
                    dead = 0
                else:
                    dead += 1
            else:
                part_path.unlink(missing_ok=True)
                dead += 1

            if reason == "done":
                print(f"  reached end of requested range.")
                break
            if dead >= MAX_DEAD_RECONNECTS:
                print(f"  ! giving up after {dead} reconnects with no progress "
                      f"(likely end of archive or a long gap).", file=sys.stderr)
                break
            if reason in ("stall", "closed", "error"):
                print(f"  reconnecting ({reason})...")
                await asyncio.sleep(1.0)
    except KeyboardInterrupt:
        print("\nInterrupted; finalizing what was captured...")

    finalize(parts, out_path, faststart=args.faststart)
    return 0


def parse_args():
    p = argparse.ArgumentParser(
        description="Export archived video from a key.rt.ru camera over WebSocket.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples:")[-1],
    )
    p.add_argument("--access-token", required=True,
                   help="oauth2.key.rt.ru access token (Bearer JWT)")
    p.add_argument("--camera", required=True, help="camera id (uuid)")
    p.add_argument("--start", required=True,
                   help="start time: unix ts, 'now', or 'YYYY-MM-DD HH:MM:SS' "
                        "(camera-local)")
    p.add_argument("--end", help="end time (same formats as --start)")
    p.add_argument("--duration", default="10m",
                   help="duration if --end not given, e.g. 90s,15m,2h,1h30m "
                        "(default: 10m)")
    p.add_argument("--speed", type=int, default=DEFAULT_SPEED,
                   help=f"pull speed multiplier (default: {DEFAULT_SPEED}; "
                        "~17x is the practical ceiling)")
    p.add_argument("--out", help="output mp4 path "
                                 "(default: archive/<camera>_<start>.mp4)")
    p.add_argument("--fragment-length", type=float, default=FRAGMENT_LENGTH_DEFAULT,
                   help="server fragment length in seconds (default: 0.5)")
    p.add_argument("--faststart", action="store_true",
                   help="for a single-session capture, run an extra ffmpeg pass "
                        "to produce a flat (non-fragmented) moov. Needs ~2x the "
                        "output size in free disk. Default: move the fMP4 as-is "
                        "(already seekable), no second copy.")
    p.add_argument("--probe", action="store_true",
                   help="connect, send seek, print the first frames, and exit")
    p.add_argument("--timeout", type=float, default=20.0,
                   help="HTTP timeout for the camera lookup (default: 20)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run_export(args))
    except requests.HTTPError as exc:
        resp = getattr(exc, "response", None)
        print(f"HTTP error: {exc}\n{resp.text if resp is not None else ''}",
              file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
