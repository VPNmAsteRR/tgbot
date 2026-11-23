#!/usr/bin/env bash
# Установка бота на сервер с systemd. Запускать от root.
#
#   BOT_TOKEN=123:abc OWNER_ID=123456 bash deploy/install.sh
#
set -euo pipefail

APP_DIR=/opt/tg-relay
SERVICE=tg-relay
SERVICE_USER=tg-relay
STATE_DIR=/var/lib/tg-relay
ENV_FILE=/etc/tg-relay.env
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ${EUID} -ne 0 ]]; then
    echo "нужен root: sudo BOT_TOKEN=... OWNER_ID=... bash $0" >&2
    exit 1
fi

if ! command -v python3 >/dev/null; then
    echo "python3 не найден. Debian/Ubuntu: apt install -y python3; RHEL: dnf install -y python3" >&2
    exit 1
fi

python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    sys.exit("нужен Python 3.10+, найден %s" % sys.version.split()[0])
PY

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -o root -g root -m 755 "${APP_DIR}"
install -m 644 "${SRC}/bot.py" "${SRC}/store.py" "${SRC}/tgapi.py" "${APP_DIR}/"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 700 "${STATE_DIR}"

if [[ ! -f "${ENV_FILE}" ]]; then
    if [[ -z "${BOT_TOKEN:-}" ]]; then
        echo "BOT_TOKEN не задан, и ${ENV_FILE} отсутствует" >&2
        exit 1
    fi
    umask 077
    {
        printf 'BOT_TOKEN=%s\n' "${BOT_TOKEN}"
        [[ -n "${OWNER_ID:-}" ]] && printf 'OWNER_ID=%s\n' "${OWNER_ID}"
        printf 'TZ=%s\n' "${TZ:-Europe/Moscow}"
    } > "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    echo "создан ${ENV_FILE}"
fi

install -m 644 "${SRC}/deploy/tg-relay.service" "/etc/systemd/system/${SERVICE}.service"
systemctl daemon-reload
systemctl enable --now "${SERVICE}.service"

sleep 1
systemctl --no-pager --lines=15 status "${SERVICE}.service" || true
echo
echo "логи: journalctl -u ${SERVICE} -f"
