#!/usr/bin/env bash
# Uninstall Companion AI OS Linux systemd service while preserving isolated user data.
set -euo pipefail

if [[ "${1:-}" == "--user" ]]; then
  systemctl --user disable --now companion.service 2>/dev/null || true
  rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/companion.service"
  systemctl --user daemon-reload
  rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/companion/app"
  echo "Removed Companion AI OS user service. User data preserved in ${XDG_DATA_HOME:-$HOME/.local/share}/companion/data"
  exit 0
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run with '--user' (user service) or 'sudo' (system service)."
  exit 1
fi

systemctl disable --now companion.service 2>/dev/null || true
rm -f /etc/systemd/system/companion.service
systemctl daemon-reload
rm -rf /opt/companion
echo "Removed Companion AI OS system service. User data preserved in /var/lib/companion"
