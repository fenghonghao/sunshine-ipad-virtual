#!/usr/bin/python3
"""Safely leave the temporary virtual-display trial without losing all output."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess

from gi.repository import Gio, GLib

UNIT = "app-dev.lizardbyte.app.Sunshine.service"
ROOT = Path.home() / ".config/sunshine"
STATE = Path.home() / ".local/state/sunshine-ipad-virtual"
DROPIN = Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd/user" / (UNIT + ".d") / "90-ipad-virtual-trial.conf"
EXPECTED = "[Service]\nExecStart=\nExecStart=/usr/bin/sunshine capture=portal encoder=vaapi adapter_name=/dev/dri/renderD128 output_name=Meta-0\n"
INTERFACE = "org.gnome.Mutter.DisplayConfig"
OBJECT_PATH = "/org/gnome/Mutter/DisplayConfig"


def run(*args):
    subprocess.run(args, check=True, timeout=45)


def display_state(bus):
    return bus.call_sync(INTERFACE, OBJECT_PATH, INTERFACE, "GetCurrentState",
                         None, None, Gio.DBusCallFlags.NONE, 5000, None).unpack()


def physical_layout(current):
    candidates = [(spec, modes) for spec, modes, props in current[1]
                  if not spec[0].startswith("Meta") and modes]
    if not candidates:
        raise RuntimeError("没有可用实体屏，请先打开笔记本盖或接回外接显示器。")
    candidates.sort(key=lambda item: item[0][0] != "DP-1")
    spec, modes = candidates[0]
    mode = next((m for m in modes if m[6].get("is-current")), None)
    mode = mode or next((m for m in modes if m[6].get("is-preferred")), modes[0])
    scale = float(mode[4])
    for logical in current[2]:
        if any(m[0] == spec[0] for m in logical[5]):
            scale = logical[2]
            break
    return [(0, 0, scale, 0, True, [(spec[0], mode[0], {})])]


def apply_physical(bus, method):
    current = display_state(bus)
    props = {"layout-mode": GLib.Variant("u", current[3].get("layout-mode", 1))}
    args = GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})",
                       (current[0], method, physical_layout(current), props))
    bus.call_sync(INTERFACE, OBJECT_PATH, INTERFACE, "ApplyMonitorsConfig",
                  args, None, Gio.DBusCallFlags.NONE, 10000, None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate only; change no files, layout or services")
    args = parser.parse_args()
    if not DROPIN.exists():
        print("临时捕获设置已不存在，无需恢复。")
        return 0
    if DROPIN.read_text() != EXPECTED:
        raise RuntimeError("临时服务配置已被修改，未覆盖这些修改。")
    backup = Path((STATE / "latest-backup.txt").read_text().strip())
    saved_override = backup / "90-physical-capture-recovery.conf"
    target_override = DROPIN.parent / saved_override.name
    if target_override.exists() and (not saved_override.exists() or
                                     target_override.read_bytes() != saved_override.read_bytes()):
        raise RuntimeError("实体屏服务配置已被修改，未覆盖这些修改。")
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    # Method 0 checks the layout without applying it. In particular, Mutter rejects
    # a closed laptop panel here, before any service, file or token is changed.
    try:
        apply_physical(bus, 0)
    except (GLib.Error, RuntimeError) as error:
        print("无法安全恢复实体屏，请先打开笔记本盖或接回外接显示器。")
        print(error)
        return 2
    if args.check:
        print("实体屏恢复配置通过验证；未修改当前布局、配置或服务。")
        return 0

    run("systemctl", "--user", "stop", UNIT)
    try:
        # Activate physical output successfully before removing virtual capture.
        apply_physical(bus, 1)
        DROPIN.unlink()
        if saved_override.exists():
            shutil.copy2(saved_override, target_override)
        token = backup / "portal_token"
        if token.exists():
            shutil.copy2(token, ROOT / "portal_token")
        elif (ROOT / "portal_token").exists():
            (ROOT / "portal_token").unlink()
        if subprocess.run(["systemctl", "--user", "is-active", "--quiet",
                           "sunshine-ipad-virtual.service"], timeout=10).returncode == 0:
            run("systemctl", "--user", "stop", "sunshine-ipad-virtual.service")
    finally:
        run("systemctl", "--user", "daemon-reload")
        run("systemctl", "--user", "start", UNIT)
    print("实体屏捕获已恢复，虚拟屏已停止。配对记录、管理密码和持久设置未修改。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
