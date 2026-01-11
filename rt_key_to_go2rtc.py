#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
rt_key_to_go2rtc.py

1. Login to key.rt.ru using phone number and password
2. Obtain accessToken
3. Fetch cameras list (cameras.json)
4. Generate go2rtc ffmpeg stream entries

Example:
  python3 rt_key_to_go2rtc.py --phone 79123456789 --password 'MyPassword' \
      --save-json cameras.json --out streams.yaml

If --out is not specified, output is printed to stdout.
"""

import argparse
import json
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

import requests


LOGIN_URL = "https://keyapis.key.rt.ru/identity/api/v1/authorization/login_by_password"
CAMERAS_URL = "https://vc.key.rt.ru/api/v1/cameras?limit=100&offset=0"

BASE_STREAM_URL = "https://live-vdk4.camera.rt.ru/stream/{id}/live.mp4"
STREAM_QUERY = "mp4-fragment-length=0.5&mp4-use-speed=0&mp4-afiller=1&token={token}"


def build_stream_url(camera_id: str, streamer_token: str) -> str:
    """
    Build ffmpeg input URL for go2rtc.
    Token is fully URL-encoded for safety.
    """
    token_encoded = quote(streamer_token, safe="")
    return (
        f"{BASE_STREAM_URL.format(id=camera_id)}"
        f"?{STREAM_QUERY.format(token=token_encoded)}"
    )


def login(phone: str, password: str, timeout: float = 20.0) -> str:
    """
    Perform login and return accessToken.
    """
    device_id = str(uuid.uuid4())

    headers = {
        "x-device-id": device_id,
        "content-type": "application/json",
        "accept": "application/json",
    }
    payload = {
        "phoneNumber": phone,
        "password": password,
    }

    response = requests.post(
        LOGIN_URL,
        headers=headers,
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()

    data = response.json()
    access_token = (data.get("data") or {}).get("accessToken")

    if not access_token:
        raise RuntimeError("Login succeeded but accessToken was not found in response")

    return access_token


def fetch_cameras(access_token: str, timeout: float = 20.0) -> dict:
    """
    Fetch cameras.json using Bearer authorization.
    """
    headers = {
        "authorization": f"Bearer {access_token}",
        "accept": "application/json",
    }

    response = requests.get(
        CAMERAS_URL,
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def generate_go2rtc_lines(
    cameras_json: dict,
    prefix: str = "rt",
    start_index: int = 1,
) -> list[str]:
    """
    Convert cameras.json into go2rtc ffmpeg stream entries.
    """
    items = (cameras_json.get("data") or {}).get("items") or []
    if not isinstance(items, list):
        raise RuntimeError("Invalid cameras JSON: data.items is not a list")

    lines = []
    index = start_index

    for item in items:
        camera_id = item.get("id")
        streamer_token = item.get("streamer_token")

        if not camera_id or not streamer_token:
            continue

        stream_url = build_stream_url(str(camera_id), str(streamer_token))
        lines.append(f"  {prefix}{index}: ffmpeg:{stream_url}")
        index += 1

    return lines


def parse_args():
    epilog = """
Examples:
  python3 rt_key_to_go2rtc.py --phone 79123456789 --password MyPassword
  python3 rt_key_to_go2rtc.py --phone 79123456789 --password MyPassword \\
      --save-json cameras.json --out streams.yaml

If --out is "-", output is printed to stdout.
"""

    parser = argparse.ArgumentParser(
        description="Login to key.rt.ru, fetch cameras list and generate go2rtc streams.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--phone", required=True, help="Phone number, e.g. 79123456789")
    parser.add_argument("--password", required=True, help="Account password")

    parser.add_argument(
        "--save-json",
        default="cameras.json",
        help="File to save fetched cameras JSON (default: cameras.json). "
             "Use '-' to disable saving.",
    )
    parser.add_argument(
        "--out",
        default="-",
        help="Output file for go2rtc streams. '-' means stdout (default).",
    )

    parser.add_argument(
        "--prefix",
        default="rt",
        help="Stream name prefix (default: rt → rt1, rt2, ...)",
    )
    parser.add_argument(
        "--start",
        type=int,
        default=1,
        help="Starting index for stream numbering (default: 1)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP request timeout in seconds (default: 20)",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        access_token = login(args.phone, args.password, timeout=args.timeout)
        cameras_json = fetch_cameras(access_token, timeout=args.timeout)

        if args.save_json != "-":
            Path(args.save_json).write_text(
                json.dumps(cameras_json, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        lines = generate_go2rtc_lines(
            cameras_json,
            prefix=args.prefix,
            start_index=args.start,
        )

        output_text = "\n".join(lines) + ("\n" if lines else "")

        if args.out == "-" or args.out == "":
            sys.stdout.write(output_text)
        else:
            Path(args.out).write_text(output_text, encoding="utf-8")

        return 0

    except requests.HTTPError as exc:
        response = getattr(exc, "response", None)
        if response is not None:
            print(
                f"HTTP error: {exc}\nResponse body:\n{response.text}",
                file=sys.stderr,
            )
        else:
            print(f"HTTP error: {exc}", file=sys.stderr)
        return 2

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
