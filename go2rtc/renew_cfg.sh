#!/usr/bin/env bash
#
# renew_cfg.sh — refresh per-camera streamer tokens and reload go2rtc.
#
# The per-camera tokens that go2rtc uses to pull the streams expire after a few
# hours. This script re-runs the generator with the long-lived access-token to
# rebuild the config, then restarts the service. It is meant to be run from cron
# (see install.sh — every 6 hours).
#
# It is fully self-locating and reads the access-token from a local file next to
# itself (access_token, chmod 600). No paths or secrets are baked into the repo.
#
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [[ ! -r "$DIR/access_token" ]]; then
    echo "renew_cfg.sh: $DIR/access_token not found (run install.sh first)" >&2
    exit 1
fi
ACCESS_TOKEN="$(cat "$DIR/access_token")"

python3 rt_key_to_go2rtc.py --access-token "$ACCESS_TOKEN" > streams.yaml
cat base.yaml streams.yaml > go2rtc.yaml
systemctl restart go2rtc
