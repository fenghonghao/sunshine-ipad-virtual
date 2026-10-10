#!/bin/bash
# One-time root setup for the phantom display.
#   sudo bash root/install.sh              # install
#   sudo bash root/install.sh --uninstall  # unplug and remove everything
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "请用 sudo 运行" >&2; exit 1; }
HERE=$(cd "$(dirname "$0")/.." && pwd)
USER_NAME=${SUDO_USER:?请从你自己的账号用 sudo 运行}
HELPER=/usr/local/bin/sunshine-phantom-drm
RULE=/etc/sudoers.d/sunshine-phantom
FW=/lib/firmware/edid/sunshine-ipad-air-13.bin

if [ "${1:-}" = --uninstall ]; then
  [ -x "$HELPER" ] && "$HELPER" off || true
  rm -f "$HELPER" "$RULE" "$FW"
  echo "已移除: $HELPER $RULE $FW"
  exit 0
fi

install -D -o root -g root -m 0644 "$HERE/edid/sunshine-ipad-air-13.bin" "$FW"
install -o root -g root -m 0755 "$HERE/root/sunshine-phantom-drm" "$HELPER"
tmp=$(mktemp)
printf '%s ALL=(root) NOPASSWD: %s on, %s off, %s status\n' "$USER_NAME" "$HELPER" "$HELPER" "$HELPER" > "$tmp"
visudo -c -f "$tmp"
install -o root -g root -m 0440 "$tmp" "$RULE"
rm -f "$tmp"
echo "已安装: $FW $HELPER $RULE"
