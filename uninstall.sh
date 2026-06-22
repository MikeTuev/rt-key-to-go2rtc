#!/usr/bin/env bash
#
# uninstall.sh — удаляет всё, что поставил install.sh:
#   - останавливает и отключает systemd-службу go2rtc, удаляет юнит;
#   - убирает cron-задание обновления токенов;
#   - удаляет каталог установки (вместе с токеном) и лог обновления.
#
# Интерактивно:  sudo ./uninstall.sh
# Без вопросов:  sudo ./uninstall.sh --install-dir /opt/go2rtc -y
#
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/go2rtc}"
ASSUME_YES=0

usage() {
    cat <<USAGE
Использование: sudo ./uninstall.sh [опции]

Опции:
  --install-dir <DIR>   каталог установки (по умолчанию: /opt/go2rtc)
  -y, --yes             не спрашивать подтверждение
  -h, --help            показать справку
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --install-dir|--dir)   INSTALL_DIR="${2:-}"; shift 2;;
        --install-dir=*|--dir=*) INSTALL_DIR="${1#*=}"; shift;;
        -y|--yes)  ASSUME_YES=1; shift;;
        -h|--help) usage; exit 0;;
        *) echo "Неизвестный аргумент: $1" >&2; usage; exit 1;;
    esac
done

SELF="$(realpath "${BASH_SOURCE[0]}")"

# --- проверка на root ---
if [[ $EUID -ne 0 ]]; then
    echo "Требуются права root. Перезапускаю через sudo..."
    exec sudo -E bash "$SELF" "$@"
fi
[[ $EUID -eq 0 ]] || { echo "Этот скрипт должен запускаться от root." >&2; exit 1; }

# защита от опасных значений
if [[ -z "$INSTALL_DIR" || "$INSTALL_DIR" == "/" ]]; then
    echo "Небезопасный INSTALL_DIR='$INSTALL_DIR'. Прерываю." >&2
    exit 1
fi

echo "Будет удалено:"
echo "  - служба go2rtc (/etc/systemd/system/go2rtc.service)"
echo "  - cron-задание ($INSTALL_DIR/renew_cfg.sh)"
echo "  - каталог $INSTALL_DIR (включая access_token)"
echo "  - /var/log/go2rtc-renew.log"
if [[ $ASSUME_YES -ne 1 ]]; then
    read -rp "Продолжить? [y/N] " ans
    [[ "$ans" == [yY]* ]] || { echo "Отменено."; exit 0; }
fi

echo "==> Останавливаю и отключаю службу..."
systemctl stop go2rtc 2>/dev/null || true
systemctl disable go2rtc 2>/dev/null || true
rm -f /etc/systemd/system/go2rtc.service
systemctl daemon-reload 2>/dev/null || true

echo "==> Убираю cron-задание..."
if crontab -l 2>/dev/null | grep -Fq "$INSTALL_DIR/renew_cfg.sh"; then
    crontab -l 2>/dev/null | grep -Fv "$INSTALL_DIR/renew_cfg.sh" | crontab -
    echo "  удалено."
else
    echo "  не найдено."
fi

echo "==> Удаляю файлы..."
rm -f /var/log/go2rtc-renew.log
if [[ -e "$INSTALL_DIR/renew_cfg.sh" || -e "$INSTALL_DIR/go2rtc" ]]; then
    rm -rf "$INSTALL_DIR"
    echo "  каталог $INSTALL_DIR удалён."
else
    echo "  $INSTALL_DIR не похож на установку go2rtc — не трогаю."
fi

echo "Готово. go2rtc удалён."
