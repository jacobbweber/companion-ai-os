#!/usr/bin/env bash
# Install or upgrade Companion AI OS as a Linux systemd daemon with strict App vs. User Data separation.
#
# Modes:
#   1) Rootless User Service (no sudo needed):
#        bash deploy/linux/install-service.sh --user [--host 127.0.0.1] [--port 8000]
#        App Code : ~/.local/share/companion/app
#        User Data: ~/.local/share/companion/data
#
#   2) System-Wide Daemon (requires sudo):
#        sudo bash deploy/linux/install-service.sh [--host 127.0.0.1] [--port 8000]
#        App Code : /opt/companion
#        User Data: /var/lib/companion
set -euo pipefail

MODE="system"
HOST=""
PORT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) MODE="user"; shift ;;
    --host) HOST="${2:-127.0.0.1}"; shift 2 ;;
    --port) PORT="${2:-8000}"; shift 2 ;;
    *) echo "Unknown argument: $1 (usage: [--user] [--host 127.0.0.1] [--port 8000])"; exit 1 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY_BIN="$(command -v python3 || true)"
[[ -n "${PY_BIN}" ]] || { echo "Error: python3 (3.12+) is required."; exit 1; }

resolve_existing_bind() {
  local unit_file="$1"
  if [[ -f "${unit_file}" ]]; then
    if [[ -z "${HOST}" ]]; then
      HOST="$(sed -n 's/.*--host \([^ ]*\).*/\1/p' "${unit_file}" | head -n 1)"
    fi
    if [[ -z "${PORT}" ]]; then
      PORT="$(sed -n 's/.*--port \([0-9]*\).*/\1/p' "${unit_file}" | head -n 1)"
    fi
  fi
  HOST="${HOST:-127.0.0.1}"
  PORT="${PORT:-8000}"
}

copy_clean_app() {
  local dest="$1"
  mkdir -p "${dest}"
  rm -rf "${dest}/companion" "${dest}/configs" "${dest}/memory" \
         "${dest}/skills" "${dest}/specs" "${dest}/surfaces"
  cp -r "${REPO_ROOT}/harness.py" "${REPO_ROOT}/.env.example" "${REPO_ROOT}/companion" \
        "${REPO_ROOT}/configs" "${REPO_ROOT}/memory" "${REPO_ROOT}/skills" \
        "${REPO_ROOT}/specs" "${REPO_ROOT}/surfaces" "${dest}/"
  rm -rf "${dest}/memory/"*.db* "${dest}/companion/__pycache__" "${dest}/surfaces/__pycache__" \
         "${dest}/configs/__pycache__" "${dest}/memory/__pycache__"
  [[ -f "${REPO_ROOT}/LICENSE" ]] && cp "${REPO_ROOT}/LICENSE" "${dest}/"
  [[ -f "${REPO_ROOT}/README.md" ]] && cp "${REPO_ROOT}/README.md" "${dest}/"
}

if [[ "${MODE}" == "user" ]]; then
  APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/companion/app"
  DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/companion/data"
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  SERVICE_DEST="${UNIT_DIR}/companion.service"

  resolve_existing_bind "${SERVICE_DEST}"
  mkdir -p "${APP_DIR}" "${DATA_DIR}" "${UNIT_DIR}"
  copy_clean_app "${APP_DIR}"
  [[ -f "${DATA_DIR}/.env" ]] || cp "${REPO_ROOT}/.env.example" "${DATA_DIR}/.env"

  cat > "${SERVICE_DEST}" <<EOF
[Unit]
Description=Companion AI OS — Rootless User Background Service
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${DATA_DIR}/.env
Environment="PYTHONUNBUFFERED=1"
Environment="COMPANION_DATA_DIR=${DATA_DIR}"
Environment="COMPANION_SELF_DEV_LOCKED=1"
ExecStart=${PY_BIN} ${APP_DIR}/harness.py --data-dir "${DATA_DIR}" --locked --web --host ${HOST} --port ${PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable companion.service >/dev/null 2>&1 || true
  systemctl --user restart companion.service
  loginctl enable-linger --no-ask-password "${USER:-$(id -un)}" >/dev/null 2>&1 || true
  echo ""
  echo "Companion AI OS (user service) installed and active!"
  echo "  Web Studio : http://${HOST}:${PORT}"
  echo "  App Code   : ${APP_DIR}"
  echo "  User Data  : ${DATA_DIR}"
  echo "  Logs       : journalctl --user -u companion -f"
  exit 0
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "Tip: Run with '--user' for a rootless per-user service, or with 'sudo' for a system-wide /opt/companion daemon:"
  echo "  bash deploy/linux/install-service.sh --user"
  echo "  sudo bash deploy/linux/install-service.sh"
  exit 1
fi

APP_DIR="/opt/companion"
DATA_DIR="/var/lib/companion"
SERVICE_DEST="/etc/systemd/system/companion.service"

resolve_existing_bind "${SERVICE_DEST}"

if ! id -u companion >/dev/null 2>&1; then
  useradd --system --home-dir "${DATA_DIR}" --create-home --shell /usr/sbin/nologin companion
fi

mkdir -p "${APP_DIR}" "${DATA_DIR}"
copy_clean_app "${APP_DIR}"
[[ -f "${DATA_DIR}/.env" ]] || cp "${REPO_ROOT}/.env.example" "${DATA_DIR}/.env"

chown -R companion:companion "${APP_DIR}" "${DATA_DIR}"
sed -e "s|--host 127.0.0.1|--host ${HOST}|g" -e "s|--port 8000|--port ${PORT}|g" -e "s|/usr/bin/python3|${PY_BIN}|g" \
    "${REPO_ROOT}/deploy/linux/companion.service" > "${SERVICE_DEST}"

systemctl daemon-reload
systemctl enable companion.service >/dev/null 2>&1 || true
systemctl restart companion.service

echo ""
echo "Companion AI OS (system daemon) installed and active!"
echo "  Web Studio : http://${HOST}:${PORT}"
echo "  App Code   : ${APP_DIR}"
echo "  User Data  : ${DATA_DIR}"
echo "  Logs       : sudo journalctl -u companion -f"
