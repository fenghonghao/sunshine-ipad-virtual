#!/bin/bash
# 安装、更新或卸载 sunshine-ipad-virtual 的用户部分（不需要 root，也不要用 sudo 运行）。
# 先装 root 部分（只需一次）：sudo bash root/install.sh
#
#   bash install.sh               安装或更新：再执行一次就是更新
#   bash install.sh --uninstall   卸载：只删除本脚本放进去的文件
#
# 可选的环境变量：
#   SUNSHINE_WEB_CREDENTIALS="用户名:密码"   Sunshine 网页的账号密码；不设置时会交互询问
#   ADAPTER_NAME=/dev/dri/by-path/...        Intel 核显的渲染节点；默认自动查找
set -euo pipefail

REPO=$(cd "$(dirname "$0")" && pwd)
DEST=$HOME/.local/share/sunshine-ipad-virtual
UNITS=$HOME/.config/systemd/user
SUNSHINE_UNIT=app-dev.lizardbyte.app.Sunshine.service
DROPIN_DIR=$UNITS/$SUNSHINE_UNIT.d
CONFIG=$HOME/.config/sunshine-ipad-virtual
SUNSHINE_CONF=$HOME/.config/sunshine/sunshine.conf
HELPER=/usr/local/bin/sunshine-phantom-drm
CONNECTOR=HDMI-A-1
FILES=(phantom/controller.py phantom/display.py phantom/contact.py phantom/sunshine_api.py
       bin/phantomctl bin/sunshine-ipad-mode)
LINKS=(phantomctl sunshine-ipad-mode)

die() { echo "错误: $*" >&2; exit 1; }

# Edits sunshine.conf in place, keeping every line it does not own. $1 = check | install | uninstall.
sunshine_conf() {
  /usr/bin/python3 - "$1" "$SUNSHINE_CONF" "$DEST/bin/phantomctl" "${ADAPTER_NAME:-}" "$CONNECTOR" <<'EOF'
import json
import sys
from pathlib import Path

action, path, phantomctl, adapter, connector = sys.argv[1:]
path = Path(path)
lines = path.read_text().splitlines() if path.exists() else []


def index(key):
    return next((i for i, line in enumerate(lines)
                 if line.partition("=")[1] and line.partition("=")[0].strip() == key), None)


def value(key):
    i = index(key)
    return None if i is None else lines[i].partition("=")[2].strip()


def put(key, new):
    i = index(key)
    if new is None:
        if i is not None:
            del lines[i]
    elif i is None:
        lines.append(f"{key} = {new}")
    else:
        lines[i] = f"{key} = {new}"


try:
    commands = json.loads(value("global_prep_cmd") or "[]")
except ValueError:
    sys.exit(f"错误: 无法解析 {path} 里的 global_prep_cmd，请先手动修正")
if action == "check":
    sys.exit(0)
commands = [c for c in commands if "phantomctl session-" not in c.get("do", "") + c.get("undo", "")]
if action == "install":
    commands.append({"do": f"{phantomctl} session-start", "undo": f"{phantomctl} session-end", "elevated": False})
    put("capture", "kms")
    put("encoder", "vaapi")
    put("output_name", connector)
    if adapter:
        put("adapter_name", adapter)
elif value("output_name") == connector:
    put("output_name", None)  # names the phantom, which no longer exists
put("global_prep_cmd", json.dumps(commands, separators=(",", ":")) if commands else None)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("".join(line + "\n" for line in lines))
EOF
}

backup_sunshine_conf() {
  [ -f "$SUNSHINE_CONF" ] || return 0
  local backup
  backup="$SUNSHINE_CONF.bak-$(date +%Y%m%d-%H%M%S)"
  cp -p "$SUNSHINE_CONF" "$backup"
  echo "已备份 $SUNSHINE_CONF -> $backup"
}

detect_adapter() {
  local r
  for r in /sys/class/drm/renderD*; do
    [ "$(basename "$(readlink -f "$r/device/driver")")" = i915 ] || continue
    echo "/dev/dri/by-path/pci-$(basename "$(readlink -f "$r/device")")-render"
    return 0
  done
}

install_all() {
  # --- 先检查，任何一项不满足都不做改动 ---
  [ "$(id -u)" != 0 ] || die "不要用 sudo 运行本脚本；root 部分请执行 sudo bash $REPO/root/install.sh"
  sudo -n "$HELPER" status >/dev/null 2>&1 || die "root 部分还没装好，请先执行: sudo bash $REPO/root/install.sh"
  systemctl --user cat "$SUNSHINE_UNIT" >/dev/null 2>&1 || die "没找到 Sunshine 的用户单元 $SUNSHINE_UNIT，请先安装 Sunshine"
  /usr/bin/python3 -c "from gi.repository import Gio, GLib" 2>/dev/null || die "缺少系统的 Python GObject 绑定（Ubuntu: sudo apt install python3-gi）"
  sunshine_conf check
  local credentials=${SUNSHINE_WEB_CREDENTIALS-}
  if [ ! -s "$CONFIG/credentials" ] && [ -z "$credentials" ]; then
    [ -t 0 ] || die "请用 SUNSHINE_WEB_CREDENTIALS=\"用户名:密码\" 提供 Sunshine 网页的账号密码"
    local user password
    read -rp "Sunshine 网页用户名: " user
    read -rsp "Sunshine 网页密码: " password
    echo
    credentials="$user:$password"
  fi
  [ -s "$CONFIG/credentials" ] || [[ $credentials == ?*:?* ]] || die "账号密码格式应为 用户名:密码"
  ADAPTER_NAME=${ADAPTER_NAME-$(detect_adapter || true)}

  # --- 程序文件 ---
  if [ "$REPO" -ef "$DEST" ]; then
    echo "代码已经在 $DEST，跳过复制"
  else
    local f
    for f in "${FILES[@]}"; do
      if [ -x "$REPO/$f" ]; then install -D -m 755 "$REPO/$f" "$DEST/$f"; else install -D -m 644 "$REPO/$f" "$DEST/$f"; fi
    done
    echo "已复制程序文件到 $DEST"
  fi
  mkdir -p "$HOME/.local/bin"
  local name
  for name in "${LINKS[@]}"; do ln -sfn "$DEST/bin/$name" "$HOME/.local/bin/$name"; done

  # --- systemd ---
  install -D -m 644 "$REPO/systemd/sunshine-phantom.service" "$UNITS/sunshine-phantom.service"
  install -D -m 644 "$REPO/systemd/50-phantom-wake.conf" "$DROPIN_DIR/50-phantom-wake.conf"
  systemctl --user daemon-reload
  systemctl --user enable sunshine-phantom.service
  echo "已安装并启用 sunshine-phantom.service（随 Sunshine 启动）"

  # --- Sunshine 网页账号密码 ---
  if [ ! -s "$CONFIG/credentials" ]; then
    install -d -m 700 "$CONFIG"
    (umask 077; printf '%s\n' "$credentials" > "$CONFIG/credentials")
    echo "已保存 Sunshine 网页账号到 $CONFIG/credentials（权限 600）"
  fi

  # --- sunshine.conf ---
  backup_sunshine_conf
  sunshine_conf install
  echo "已更新 $SUNSHINE_CONF: capture=kms encoder=vaapi output_name=$CONNECTOR adapter_name=${ADAPTER_NAME:-（未找到，未设置）}"

  if systemctl --user is-active -q "$SUNSHINE_UNIT"; then
    if "$DEST/bin/phantomctl" status 2>/dev/null | grep -q '"session_active": true'; then
      echo "正在串流：控制器和 Sunshine 等串流结束后再重启"
    else
      systemctl --user restart sunshine-phantom.service
    fi
    echo "请重启 Sunshine 让新配置生效（会断开正在进行的串流）: systemctl --user restart $SUNSHINE_UNIT"
  else
    echo "完成。从应用菜单打开 Sunshine 即可，控制器会跟着启动。"
  fi
}

uninstall_all() {
  systemctl --user disable --now sunshine-phantom.service || true
  rm -f "$UNITS/sunshine-phantom.service" "$DROPIN_DIR/50-phantom-wake.conf"
  rmdir --ignore-fail-on-non-empty "$DROPIN_DIR" 2>/dev/null || true
  systemctl --user daemon-reload || true

  local name f
  for name in "${LINKS[@]}"; do
    [ "$(readlink "$HOME/.local/bin/$name" || true)" = "$DEST/bin/$name" ] && rm -f "$HOME/.local/bin/$name"
  done
  if [ "$REPO" -ef "$DEST" ]; then
    echo "代码目录就是 $DEST 本身，保留其中的文件"
  else
    for f in "${FILES[@]}"; do rm -f "$DEST/$f"; done
    rm -rf "$DEST/phantom/__pycache__"
    rmdir --ignore-fail-on-non-empty "$DEST/phantom" "$DEST/bin" "$DEST" 2>/dev/null || true
  fi
  rm -f "$CONFIG/credentials" "$CONFIG/mode"
  rmdir --ignore-fail-on-non-empty "$CONFIG" 2>/dev/null || true

  if [ -f "$SUNSHINE_CONF" ]; then
    backup_sunshine_conf
    sunshine_conf uninstall
    echo "已从 $SUNSHINE_CONF 移除本项目的 global_prep_cmd 和 output_name；capture/encoder/adapter_name 是 Sunshine 的普通设置，已保留"
  fi
  echo "完成。root 部分请执行: sudo bash $REPO/root/install.sh --uninstall；然后重启 Sunshine。"
}

case "${1:-}" in
  "") install_all ;;
  --uninstall) uninstall_all ;;
  *) echo "用法: bash install.sh [--uninstall]" >&2; exit 2 ;;
esac
