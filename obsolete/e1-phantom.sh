#!/bin/bash
# E1 trial: plug/unplug the phantom iPad display on Intel HDMI-A-1. Run with sudo.
#   sudo bash e1-phantom.sh on    # plug; auto-reverts unless Enter is pressed within 60 s
#   sudo bash e1-phantom.sh off   # unplug and clear the EDID override
set -euo pipefail

CONN=HDMI-A-1
SYSFS=/sys/class/drm/card1-$CONN
PARAM=/sys/module/drm/parameters/edid_firmware
FW=edid/sunshine-ipad-air-13.bin
SRC=/home/bsb/.local/share/sunshine-ipad-virtual/$FW

[ "$(id -u)" = 0 ] || { echo "请用 sudo 运行" >&2; exit 1; }

show() {
  echo "status=$(cat $SYSFS/status) enabled=$(cat $SYSFS/enabled) edid_firmware='$(cat $PARAM)'"
  echo "modes: $(tr '\n' ' ' < $SYSFS/modes)"
  dmesg | grep -i -E "edid|$CONN|hdmi|scdc|scrambl" | tail -n 8 || true
}

off() {
  echo detect > "$SYSFS/status"
  printf '\0' > "$PARAM"
  echo "已撤销：$CONN 恢复自动检测，EDID 覆盖已清空"
}

case "${1:-}" in
  on)
    # A plain read reports the hardware probe; "connected" here means a real sink or a previous run.
    [ "$(cat $SYSFS/status)" = disconnected ] || { echo "$CONN 当前不是 disconnected，先运行 off" >&2; exit 1; }
    install -D -m 0644 "$SRC" "/lib/firmware/$FW"
    printf '%s' "$CONN:$FW" > "$PARAM"
    echo on > "$SYSFS/status"
    sleep 3
    show
    if read -r -t 60 -p "看到新显示器且 DP-1 正常？按回车保留（60 秒无响应自动撤销）: "; then
      echo "已保留。检查完后运行: sudo bash $0 off"
    else
      echo; off
    fi
    ;;
  off) off; show ;;
  *) echo "用法: sudo bash $0 on|off" >&2; exit 2 ;;
esac
