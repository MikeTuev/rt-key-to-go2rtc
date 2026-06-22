#!/usr/bin/env bash
#
# install.sh — установка go2rtc + автообновление токенов камер «Ростелеком Ключ».
#
# Что делает:
#   1. спрашивает access-token (и показывает, где его взять в браузере);
#   2. определяет архитектуру и скачивает нужный go2rtc (amd64/arm64/arm/i386);
#   3. ставит зависимости через apt (python3, python3-requests, ffmpeg, curl);
#   4. ставит systemd-службу go2rtc;
#   5. ставит cron, который каждые 6 часов обновляет токены камер и
#      перезапускает службу (per-camera токены живут всего несколько часов).
#
# Авторизация — ТОЛЬКО по access-token. Вход по телефону/паролю не используем:
# он может требовать капчу.
#
# Токен НЕ хранится в репозитории — он сохраняется локально в
# <INSTALL_DIR>/access_token (chmod 600).
#
# Интерактивно:        sudo ./install.sh
# Авто (без вопросов):  sudo ./install.sh --token eyJ... [--install-dir /opt/go2rtc] [--arch arm64] -y
# Через переменные:     ACCESS_TOKEN=eyJ... INSTALL_DIR=/opt/go2rtc sudo -E ./install.sh
#
set -euo pipefail

GO2RTC_RELEASE_BASE="https://github.com/AlexxIT/go2rtc/releases/latest/download"
CRON_SCHEDULE="0 */6 * * *"
API_PORT=1984
RTSP_PORT=8554

# значения по умолчанию (можно переопределить ENV или аргументами)
INSTALL_DIR="${INSTALL_DIR:-/opt/go2rtc}"
ACCESS_TOKEN="${ACCESS_TOKEN:-}"
ARCH_OVERRIDE="${GO2RTC_ARCH:-}"
ASSUME_YES=0

usage() {
    cat <<USAGE
Использование: sudo ./install.sh [опции]

Опции:
  --token <TOKEN>        access-token (иначе будет запрошен интерактивно)
  --install-dir <DIR>    каталог установки (по умолчанию: /opt/go2rtc)
  --arch <amd64|arm64|arm|i386>
                         принудительно задать архитектуру go2rtc
                         (по умолчанию определяется автоматически)
  -y, --yes              не задавать вопросов (для авто-установки)
  -h, --help             показать эту справку

Переменные окружения: ACCESS_TOKEN, INSTALL_DIR, GO2RTC_ARCH (с sudo -E).
USAGE
}

# --- разбор аргументов (до sudo, чтобы --help работал без root) ---
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --token)        ACCESS_TOKEN="${2:-}"; shift 2;;
            --token=*)      ACCESS_TOKEN="${1#*=}"; shift;;
            --install-dir|--dir)   INSTALL_DIR="${2:-}"; shift 2;;
            --install-dir=*|--dir=*) INSTALL_DIR="${1#*=}"; shift;;
            --arch)         ARCH_OVERRIDE="${2:-}"; shift 2;;
            --arch=*)       ARCH_OVERRIDE="${1#*=}"; shift;;
            -y|--yes)       ASSUME_YES=1; shift;;
            -h|--help)      usage; exit 0;;
            *) echo "Неизвестный аргумент: $1" >&2; usage; exit 1;;
        esac
    done
}
parse_args "$@"

SELF="$(realpath "${BASH_SOURCE[0]}")"
REPO_DIR="$(dirname "$SELF")"

# --- проверка на запуск от root (systemd, cron, $INSTALL_DIR, /var/log) ---
if [[ $EUID -ne 0 ]]; then
    echo "Требуются права root. Перезапускаю через sudo..."
    exec sudo -E bash "$SELF" "$@"
fi
[[ $EUID -eq 0 ]] || { echo "Этот скрипт должен запускаться от root." >&2; exit 1; }

# --- определение архитектуры go2rtc ---
detect_arch() {
    case "$(uname -m)" in
        x86_64|amd64)          echo "amd64";;
        aarch64|arm64)         echo "arm64";;
        armv7l|armv6l|armhf|arm) echo "arm";;
        i386|i486|i586|i686)   echo "i386";;
        *) return 1;;
    esac
}
if [[ -n "$ARCH_OVERRIDE" ]]; then
    ARCH="$ARCH_OVERRIDE"
else
    ARCH="$(detect_arch)" || {
        echo "Не удалось определить архитектуру ($(uname -m)). Задай вручную: --arch amd64|arm64|arm|i386" >&2
        exit 1
    }
fi
GO2RTC_BIN="go2rtc_linux_${ARCH}"
GO2RTC_URL="$GO2RTC_RELEASE_BASE/$GO2RTC_BIN"

echo "============================================================"
echo " Установка go2rtc + автообновление токенов (Ростелеком Ключ)"
echo "   архитектура: $(uname -m) → $GO2RTC_BIN"
echo "   каталог:     $INSTALL_DIR"
echo "============================================================"
echo
echo "Где взять access-token (используем ТОЛЬКО токен — вход по телефону"
echo "может требовать капчу, поэтому он не используется):"
echo
echo "  1. Открой в браузере  https://key.rt.ru/main/pwa/dashboard  и войди."
echo "  2. Нажми F12 → вкладка Network (Сеть)."
echo "  3. Найди запрос  barrier  и в его заголовках возьми строку:"
echo "         Authorization: Bearer <ТОКЕН>"
echo "  4. Скопируй сам <ТОКЕН> — длинная строка вида  eyJ..."
echo "     (подробнее: archive/README.md)"
echo

# --- получаем токен (из аргумента/ENV или интерактивно) ---
normalize_token() {
    ACCESS_TOKEN="$(printf '%s' "$ACCESS_TOKEN" | tr -d '[:space:]')"
    ACCESS_TOKEN="${ACCESS_TOKEN#Bearer}"
}
if [[ -z "$ACCESS_TOKEN" ]]; then
    if [[ ! -t 0 ]]; then
        echo "Нет терминала для ввода. Передай токен:  --token eyJ...  или  ACCESS_TOKEN=eyJ... sudo -E ./install.sh" >&2
        exit 1
    fi
    while [[ -z "$ACCESS_TOKEN" ]]; do
        read -rsp "Вставь access-token и нажми Enter: " ACCESS_TOKEN
        echo
        normalize_token
        if [[ "$ACCESS_TOKEN" != *.*.* ]]; then
            echo "  Не похоже на токен (ожидается eyJ... с точками). Ещё раз."
            ACCESS_TOKEN=""
        fi
    done
else
    normalize_token
fi

# --- зависимости через apt + проверка ---
echo "==> Устанавливаю зависимости (python3, python3-requests, ffmpeg, curl)..."
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        python3 python3-requests ffmpeg curl >/dev/null || true
else
    echo "  apt-get не найден — поставь вручную: python3, python3-requests, ffmpeg, curl."
fi
missing=()
command -v python3 >/dev/null 2>&1 || missing+=("python3")
command -v ffmpeg  >/dev/null 2>&1 || missing+=("ffmpeg")
command -v curl    >/dev/null 2>&1 || missing+=("curl")
python3 -c 'import requests' 2>/dev/null || missing+=("python3-requests")
if (( ${#missing[@]} )); then
    echo "  Отсутствуют зависимости: ${missing[*]}. Установи их и запусти снова." >&2
    exit 1
fi

# --- проверяем токен (заодно показываем число камер) ---
echo "==> Проверяю токен (запрашиваю список камер)..."
CAM_COUNT="$(python3 - "$ACCESS_TOKEN" <<'PY'
import sys, json, urllib.request
tok = sys.argv[1]
req = urllib.request.Request(
    "https://vc.key.rt.ru/api/v1/cameras?limit=100&offset=0",
    headers={"authorization": f"Bearer {tok}", "accept": "application/json"})
try:
    data = json.load(urllib.request.urlopen(req, timeout=20))
    print(len((data.get("data") or {}).get("items") or []))
except Exception as e:
    print("ERR:" + str(e))
PY
)"
if [[ "$CAM_COUNT" == ERR:* || -z "$CAM_COUNT" ]]; then
    echo "  Токен не принят: ${CAM_COUNT#ERR:}" >&2
    echo "  Проверь токен и запусти снова." >&2
    exit 1
fi
echo "  OK — камер найдено: $CAM_COUNT"

# --- раскладываем файлы ---
echo "==> Устанавливаю в $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
install -m 644 "$REPO_DIR/rt_key_to_go2rtc.py" "$INSTALL_DIR/rt_key_to_go2rtc.py"
install -m 644 "$REPO_DIR/go2rtc/base.yaml"    "$INSTALL_DIR/base.yaml"
install -m 755 "$REPO_DIR/go2rtc/renew_cfg.sh" "$INSTALL_DIR/renew_cfg.sh"

# токен — локально, с правами 600
( umask 077; printf '%s' "$ACCESS_TOKEN" > "$INSTALL_DIR/access_token" )
chmod 600 "$INSTALL_DIR/access_token"

# --- скачиваем go2rtc под нужную архитектуру ---
echo "==> Скачиваю go2rtc ($GO2RTC_BIN)..."
curl -fSL "$GO2RTC_URL" -o "$INSTALL_DIR/go2rtc"
chmod +x "$INSTALL_DIR/go2rtc"

# --- systemd-служба (пути подставляются здесь, в репозитории их нет) ---
echo "==> Ставлю systemd-службу go2rtc..."
cat > /etc/systemd/system/go2rtc.service <<EOF
[Unit]
Description=go2rtc (RTSP / WebRTC / HTTP gateway)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/go2rtc -config $INSTALL_DIR/go2rtc.yaml
Restart=always
RestartSec=2
StandardOutput=journal
StandardError=journal
LimitNOFILE=65536
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable go2rtc >/dev/null 2>&1 || true

# --- первый запуск: renew генерирует go2rtc.yaml и (пере)запускает службу ---
echo "==> Генерирую конфиг и запускаю go2rtc..."
"$INSTALL_DIR/renew_cfg.sh"

# --- cron (root), каждые 6 часов ---
echo "==> Ставлю cron (каждые 6 часов)..."
CRON_CMD="$INSTALL_DIR/renew_cfg.sh >> /var/log/go2rtc-renew.log 2>&1"
# crontab -l завершается с ошибкой, если crontab ещё нет — нейтрализуем через || true,
# иначе set -e/pipefail обрывает скрипт и cron не добавляется.
EXISTING_CRON="$(crontab -l 2>/dev/null || true)"
if grep -Fq "$CRON_CMD" <<<"$EXISTING_CRON"; then
    echo "  cron уже стоит."
else
    {
        [[ -n "$EXISTING_CRON" ]] && printf '%s\n' "$EXISTING_CRON"
        echo "$CRON_SCHEDULE $CRON_CMD"
    } | crontab -
    echo "  cron добавлен: $CRON_SCHEDULE $CRON_CMD"
fi

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "============================================================"
echo " Готово!"
echo
echo "   Открой go2rtc в браузере:"
echo "        http://localhost:$API_PORT"
if [[ -n "$HOST_IP" ]]; then
echo "        http://$HOST_IP:$API_PORT     (с другого устройства в сети)"
fi
echo
echo "   RTSP-потоки:  rtsp://localhost:$RTSP_PORT/rt1, .../rt2, ..."
echo "   Служба:       systemctl status go2rtc   |   journalctl -u go2rtc -f"
echo "   Каталог:      $INSTALL_DIR"
echo "   Токены обновляются автоматически каждые 6 ч (cron → renew_cfg.sh)."
echo "   Удаление:     sudo ./uninstall.sh"
echo "============================================================"
