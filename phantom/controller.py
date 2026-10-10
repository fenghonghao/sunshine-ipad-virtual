#!/usr/bin/python3
"""Phantom iPad display controller (systemd user service).

Plugs the phantom HDMI-A-1 while a Moonlight session is active, so the iPad behaves like an
external monitor, and unplugs it when the session ends (the stream ends, Sunshine exits or
restarts, the controller stops or restarts), locking first if the iPad was the only lit screen.
Power, lid and suspend stay with logind and GNOME: an unplugged phantom no longer counts as an
external display.
"""
import glob
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gi.repository import Gio, GLib, GLibUnix  # noqa: E402

import contact  # noqa: E402
import display  # noqa: E402
import sunshine_api  # noqa: E402

HELPER = "/usr/local/bin/sunshine-phantom-drm"
RUNTIME = Path(os.environ["XDG_RUNTIME_DIR"])
SOCKET = RUNTIME / "sunshine-phantom.sock"
SAVED = RUNTIME / "sunshine-phantom-physical.json"
MODE_FILE = Path.home() / ".config/sunshine-ipad-virtual/mode"
SUNSHINE_UNIT = "app-dev.lizardbyte.app.Sunshine.service"
HELPER_RETRY = 30  # seconds to wait before retrying a failed helper call


def log(*args):
    print(*args, flush=True)


def read_mode():
    try:
        mode = MODE_FILE.read_text().strip()
    except OSError:
        return "primary"
    return mode if mode in ("primary", "extend") else "primary"


def lid_closed():
    for state in glob.glob("/proc/acpi/button/lid/*/state"):
        try:
            if "closed" in Path(state).read_text():
                return True
        except OSError:
            continue
    return False


def phantom_forced():
    """Mutter-independent check: the unwired i915 HDMI-A-1 only reads connected when forced."""
    for status in glob.glob("/sys/class/drm/card*-HDMI-A-1/status"):
        card = Path(os.path.realpath(status)).parent.parent  # .../drm/cardN (class entries are symlinks)
        driver = os.path.basename(os.path.realpath(card / "device/driver"))
        if driver == "i915" and Path(status).read_text().strip() == "connected":
            return True
    return False


class Controller:
    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.session_active = False
        self.helper_blocked_until = 0.0
        self.pending = None
        self.journal = None
        self.journal_buffer = b""
        self.saved_physical = self.load_saved()
        self.woke_at = None        # when we woke blanked screens for a Moonlight contact
        self.last_contact = 0.0
        self.sunshine_pid = None   # Sunshine's MainPID; a change means a new start that will re-probe
        self.probe_pending = True  # Sunshine has not yet probed encoders since it started

    # --- persistence of the last real-screen layout (survives controller restarts) ---
    def load_saved(self):
        try:
            data = json.loads(SAVED.read_text())
        except (OSError, ValueError):
            return []
        return [(l[0], l[1], float(l[2]), l[3], bool(l[4]), [(m[0], m[1], {}) for m in l[5]]) for l in data]

    def remember_physical(self, state):
        physical = display.physical_logical(state)
        if physical:
            self.saved_physical = physical
            SAVED.write_text(json.dumps(physical))

    # --- primitives ---
    def helper(self, action):
        if action == "on" and time.monotonic() < self.helper_blocked_until:
            return False
        result = subprocess.run(["sudo", "-n", HELPER, action], capture_output=True, text=True, timeout=15)
        log(f"helper {action}: rc={result.returncode} {result.stdout.strip()} {result.stderr.strip()}")
        if result.returncode != 0:
            self.helper_blocked_until = time.monotonic() + HELPER_RETRY
        return result.returncode == 0

    def state(self):
        return display.get_state(self.bus)

    def wait_for(self, predicate, timeout=8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.state()
            if predicate(current):
                return current
            time.sleep(0.1)
        return None

    def locked(self):
        # logind's LockedHint, not ScreenSaver.GetActive: GetActive drops to false while the unlock
        # prompt is up. From a user service, session/auto is the user's graphical session.
        return self.system.call_sync("org.freedesktop.login1", "/org/freedesktop/login1/session/auto",
                                     "org.freedesktop.DBus.Properties", "Get",
                                     GLib.Variant("(ss)", ("org.freedesktop.login1.Session", "LockedHint")),
                                     None, Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]

    def lock(self):
        try:
            if self.locked():
                return  # Lock replies only when the lock screen appears, so on a locked session it hangs
        except GLib.Error as error:
            log("lock state unknown, locking anyway:", error)
        try:
            self.bus.call_sync("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver",
                               "Lock", None, None, Gio.DBusCallFlags.NONE, 5000, None)
        except GLib.Error as error:
            log("lock failed:", error)

    def lock_if_ipad_only(self):
        """Lock when the iPad was the only lit screen: the desktop is about to reappear on screens nobody watched.

        With real screens lit alongside it (extend) the unlocked desktop was on show all along, and unplugging
        a real monitor never locks either (user 2026-10-10).
        """
        try:
            if display.physical_logical(self.state()):
                return
        except GLib.Error as error:
            log("layout unknown, locking:", error)
        self.lock()

    # --- the one place where decisions become actions ---
    def schedule(self, *_args):
        if self.pending is None:
            self.pending = GLib.timeout_add(300, self.evaluate)

    def evaluate(self, relayout=False):
        self.pending = None
        try:
            self.reconcile(lid_closed(), relayout)
        except (GLib.Error, OSError, subprocess.SubprocessError) as error:
            log("reconcile failed:", error)
            if not self.session_active and phantom_forced():
                self.helper("off")  # a leftover phantom counts as an external display and blocks lid suspend
        return False

    def reconcile(self, lid, relayout):
        current = self.state()
        present = display.phantom_monitor(current) is not None
        plugged = False
        if self.session_active and not present:
            self.remember_physical(current)
            if not self.helper("on"):
                return
            current = self.wait_for(lambda s: display.phantom_monitor(s) is not None)
            if current is None:
                log("phantom did not appear in Mutter within 8 s")
                return
            relayout = plugged = True
        elif not self.session_active and present:
            self.helper("off")
            self.wait_for(lambda s: display.phantom_monitor(s) is None)
            # Mutter re-lights real screens by itself after ~5 s, in its default arrangement and with the
            # eDP-1 backlight possibly bound to nvidia_0 (B3): wait for that, then apply our own layout.
            settled = self.wait_for(lambda s: bool(display.physical_logical(s))) or self.state()
            if display.saved_matches(settled, lid, self.saved_physical):
                layout = display.restore_layout(settled, lid, self.saved_physical)
            elif not display.physical_logical(settled):
                layout = display.restore_layout(settled, lid, [])
            else:
                layout = None  # screens changed during the session: keep Mutter's choice
            if layout:
                log("restoring", layout)
                display.apply(self.bus, settled, layout)
            return
        if self.session_active and relayout:
            current = self.state()
            # Right after the plug Mutter shows its own default arrangement: keep the snapshot taken before it.
            if not plugged and display.physical_logical(current):
                self.remember_physical(current)
            layout = display.desired_layout(current, read_mode(), lid, self.saved_physical)
            log("applying", read_mode(), layout)
            display.apply(self.bus, current, layout)

    # --- session lifecycle (driven by Sunshine prep-cmd through phantomctl) ---
    def session_start(self):
        self.session_active = True
        self.probe_pending = False  # the prep-cmd runs only after Sunshine's probe succeeded
        try:
            display.wake(self.bus)  # connecting while blanked: show the lock screen on the iPad right away
        except GLib.Error as error:
            log("wake failed:", error)
        self.evaluate(relayout=True)
        return "ready phantom" if phantom_forced() else "ready no-phantom"

    def session_end(self):
        self.session_active = False
        self.lock_if_ipad_only()  # D1: before the windows move back to the real screens
        self.evaluate()
        return "ended"

    def status(self):
        lid = lid_closed()
        try:
            physical = display.physical_available(self.state(), lid)
        except GLib.Error:
            physical = None
        return json.dumps({"lid_closed": lid, "session_active": self.session_active,
                           "physical_available": physical, "phantom_plugged": phantom_forced(),
                           "probe_pending": self.probe_pending, "mode": read_mode()})

    # --- control socket ---
    def start_socket(self):
        SOCKET.unlink(missing_ok=True)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(SOCKET))
        os.chmod(SOCKET, 0o600)
        self.server.listen(4)
        GLib.io_add_watch(self.server.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN, self.on_client)

    def on_client(self, _fd, _condition):
        connection, _ = self.server.accept()
        after = None
        with connection:
            connection.settimeout(5)
            try:
                command = connection.recv(256).decode().strip()
                if command == "session-end":
                    # Sunshine's undo (phantomctl session-end) only queues this and exits without reading the
                    # reply (see bin/phantomctl); reply before the slow cleanup anyway so no caller waits on it.
                    reply, after = "ended", self.session_end
                else:
                    reply = {"session-start": self.session_start,
                             "reapply": lambda: self.evaluate(relayout=True) or "ok",
                             "status": self.status}.get(command, lambda: "unknown")()
            except Exception as error:  # one bad request must not kill the daemon
                command, reply = "?", f"error {error}"
            log(f"ctl {command!r} -> {reply}")
            try:
                connection.sendall((reply + "\n").encode())
            except OSError:
                pass
        if after:
            try:
                after()  # a session-start queued meanwhile waits on the socket, so the order still holds
            except Exception as error:  # an exception here would remove this watch and deafen the daemon
                log(f"{command} failed after the reply:", error)
        return True

    # --- Sunshine journal: a stream end (sunshine_api.is_disconnect) -> close the app so its undo runs ---
    def start_journal(self):
        self.journal = subprocess.Popen(
            ["journalctl", "--user", "-f", "-n", "0", "-o", "cat", "-u", SUNSHINE_UNIT],
            stdout=subprocess.PIPE)
        GLib.io_add_watch(self.journal.stdout.fileno(), GLib.PRIORITY_DEFAULT,
                          GLib.IO_IN | GLib.IO_HUP, self.on_journal)

    def on_journal(self, fd, _condition):
        data = os.read(fd, 65536)
        if not data:
            log("journalctl ended; restarting in 2 s")
            GLib.timeout_add_seconds(2, lambda: self.start_journal() and False)
            return False
        *lines, self.journal_buffer = (self.journal_buffer + data).split(b"\n")
        for raw in lines:
            if sunshine_api.is_disconnect(raw.decode(errors="replace")) and self.session_active:
                self.close_app_async()
        return True

    def end_leftover_session(self):
        """A phantom at start means the previous controller died mid-session; end that session as well.

        Otherwise Sunshine keeps streaming from the vanished screen (a frozen iPad) once reconcile unplugs it.
        """
        if phantom_forced():
            log("phantom left by an earlier controller: ending that session and closing the Sunshine app")
            self.lock_if_ipad_only()
            self.close_app_async()

    def stop(self):
        """SIGTERM (logout, systemctl stop/restart): no successor may come, so end a live session here.

        end_leftover_session still covers SIGKILL, which leaves no chance to run this.
        """
        try:
            if phantom_forced():
                log("stopping with the phantom plugged: ending the session, closing the Sunshine app, unplugging")
                self.lock_if_ipad_only()
                sunshine_api.close_app(timeout=10)  # its undo only queues session-end on our socket: no deadlock
                self.helper("off")
        except (OSError, subprocess.SubprocessError) as error:
            log("cleanup on stop failed:", error)
        finally:
            self.loop.quit()
        return GLib.SOURCE_REMOVE

    def close_app_async(self):
        # close_app can block for its 30 s HTTP timeout (Sunshine busy or down): never block the loop on it.
        threading.Thread(target=self.close_in_background, daemon=True).start()

    def close_in_background(self):
        if not sunshine_api.close_app():
            GLib.idle_add(self.fallback_end)

    def fallback_end(self):
        log("closing the Sunshine app failed; ending the session locally")
        self.session_end()
        return False

    # --- Moonlight contact: light blanked screens before Sunshine's pre-launch probe ---
    def contact_tick(self):
        try:
            text = Path("/proc/net/tcp").read_text() + Path("/proc/net/tcp6").read_text()
            connected = bool(contact.remote_peers(text))
            now = time.monotonic()
            if connected:
                self.last_contact = now
            blanked = display.power_save_mode(self.bus) != 0
            action = contact.contact_action(connected, blanked, self.locked(), self.session_active,
                                            self.woke_at, self.last_contact, now,
                                            probe_pending=self.probe_pending)
            if action == "wake":
                log("Moonlight contact while blanked: waking screens for Sunshine's probe")
                display.wake(self.bus)
                self.woke_at = now
            elif action == "reblank":
                log("no session followed the contact: blanking the screens we woke")
                display.set_power_save(self.bus, 3)
                self.woke_at = None
            elif action == "forget":
                self.woke_at = None
        except (OSError, GLib.Error) as error:
            log("contact check failed:", error)
        return True

    # --- Sunshine restarts: a new start re-probes encoders; a lost one leaves no undo behind ---
    def watch_sunshine(self):
        try:
            pid = self.sunshine_main_pid()
        except GLib.Error as error:
            log("Sunshine check failed:", error)
            return True
        if pid != self.sunshine_pid:
            known = self.sunshine_pid is not None
            log(f"Sunshine MainPID {self.sunshine_pid} -> {pid}: its next launch will probe encoders")
            self.sunshine_pid, self.probe_pending = pid, True
            if known and self.session_active:
                log("Sunshine exited or restarted during a session: ending it here")
                self.session_end()
        return True

    def sunshine_main_pid(self):
        unit = self.bus.call_sync("org.freedesktop.systemd1", "/org/freedesktop/systemd1",
                                  "org.freedesktop.systemd1.Manager", "LoadUnit", GLib.Variant("(s)", (SUNSHINE_UNIT,)),
                                  None, Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]
        return self.bus.call_sync("org.freedesktop.systemd1", unit, "org.freedesktop.DBus.Properties", "Get",
                                  GLib.Variant("(ss)", ("org.freedesktop.systemd1.Service", "MainPID")),
                                  None, Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]

    def run(self):
        self.loop = GLib.MainLoop()
        GLibUnix.signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, self.stop)
        self.start_socket()
        self.start_journal()
        self.bus.signal_subscribe(None, display.IFACE, "MonitorsChanged", display.PATH, None,
                                  Gio.DBusSignalFlags.NONE, self.schedule)
        self.watch_sunshine()
        GLib.timeout_add_seconds(1, self.watch_sunshine)
        GLib.timeout_add_seconds(1, self.contact_tick)
        GLib.timeout_add_seconds(2, lambda: self.evaluate() or True)  # retries a failed plug/unplug
        self.end_leftover_session()
        self.evaluate()  # reconcile at start: unplugs a phantom left plugged by a crash
        self.loop.run()


if __name__ == "__main__":
    Controller().run()
