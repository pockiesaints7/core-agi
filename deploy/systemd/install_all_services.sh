#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  exec sudo -E bash "$0" "$@"
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$ROOT_DIR/../.." && pwd)"
SYSTEMD_DIR="/etc/systemd/system"

install_unit() {
  local source_path="$1"
  local unit_name="$2"

  if [[ ! -f "$source_path" ]]; then
    echo "Missing unit source: $source_path" >&2
    exit 1
  fi

  install -D -m 0644 "$source_path" "$SYSTEMD_DIR/$unit_name"
}

install_unit "$REPO_ROOT/deploy/systemd/core-agi.service" "core-agi.service"
install_unit "$REPO_ROOT/deploy/systemd/core-trading-bot.service" "core-trading-bot.service"
install_unit "$REPO_ROOT/deploy/systemd/specter-alpha.service" "specter-alpha.service"

systemctl daemon-reload
systemctl enable core-agi.service core-trading-bot.service specter-alpha.service
systemctl restart core-agi.service core-trading-bot.service specter-alpha.service

printf '\nInstalled services:\n'
systemctl --no-pager --full status core-agi.service core-trading-bot.service specter-alpha.service | sed -n '1,120p'
