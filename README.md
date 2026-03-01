# RT Key → go2rtc Stream Generator

This script fetches the list of cameras used in **Rostelecom Key** (“Ростелеком Ключ”) and generates **go2rtc-compatible stream configuration entries** for further streaming via **RTSP / WebRTC / HLS / MSE**.

The script automates the process of turning Rostelecom Key cloud cameras into local RTSP streams using **go2rtc**.

---

## What the Script Does

1. Authenticates to Rostelecom Key using **phone/password** or a ready **access_token**
2. Retrieves the cameras list (`cameras.json`)
3. Extracts camera IDs and streamer tokens
4. Generates `ffmpeg:` stream entries for **go2rtc**

---

## Output Example

The script generates stream definitions like:

```yaml
streams:
  rt1: ffmpeg:https://live-vdk4.camera.rt.ru/stream/<camera_id>/live.mp4?...&token=<streamer_token>
  rt2: ffmpeg:...
```

These streams can then be exposed locally via RTSP or accessed through go2rtc Web UI.

---

## Example `go2rtc.yaml`

```yaml
rtsp:
  listen: ":8554"

streams:
  rt1: ffmpeg:...
  rt2: ffmpeg:...
```

---

## Requirements

* Python **3.8 or newer**
* Python package: `requests`
* `go2rtc` binary

Install Python dependency:

```bash
pip install requests
```

---

## Download go2rtc

Download the latest release from GitHub:

[https://github.com/AlexxIT/go2rtc/releases](https://github.com/AlexxIT/go2rtc/releases)

Example for Linux x64:

```bash
wget https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_linux_amd64
chmod +x go2rtc_linux_amd64
mv go2rtc_linux_amd64 go2rtc
```

---

## Usage

### 1. Run the Script

```bash
python3 rt_key_to_go2rtc.py --phone 79123456789 --password your_password
```

This command will:

* Log in to Rostelecom Key
* Fetch the cameras list
* Print go2rtc stream entries to standard output

Optional usage with files:

```bash
python3 rt_key_to_go2rtc.py \
  --phone 79123456789 \
  --password your_password \
  --save-json cameras.json \
  --out streams.yaml
```

Authorization with an existing token:

```bash
python3 rt_key_to_go2rtc.py --access-token your_access_token
```

Options:

* `--save-json` — save fetched cameras list to a file
* `--out` — save generated go2rtc stream entries to a file
* `--access-token` — use a pre-obtained token (instead of `--phone` + `--password`)
* If `--out` is not specified, output is printed to the console

---

## Create `go2rtc.yaml`

Minimal configuration example:

```yaml
rtsp:
  listen: ":8554"

streams:
```

Paste the generated stream entries under the `streams:` section.

---

## Start go2rtc

```bash
./go2rtc
```

---

## Access Streams

### Web Interface

Open in browser:

[http://localhost:1984](http://localhost:1984)

### RTSP Access

Example RTSP URL:

rtsp://localhost:8554/rt1

---

## Notes

* If something wrong please make sure you can login here https://key.rt.ru/main/pwa/dashboard
* A **random `x-device-id` UUID** is generated on each login
* `streamer_token` is automatically URL-encoded
* Only cameras available in your Rostelecom Key account are included
* Credentials or tokens are passed via command line — be careful with shell history
* Inspired by https://github.com/IokReal/intercom_for_rtc

---

## Disclaimer

This project is **unofficial** and not affiliated with Rostelecom.
Use it only with accounts and cameras you are authorized to access.
