import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gi.repository import GLib

import controller
from test_display import (DP_LOGICAL, DP_MON, EDP_ON, MUTTER_HOTPLUG_DEFAULT, PH, PH_MON, USER_LAYOUT, USER_LOGICAL,
                          state)


def make(previous_pid, current_pid, session_active):
    """A controller with no buses: only what watch_sunshine touches."""
    c = controller.Controller.__new__(controller.Controller)
    c.sunshine_pid, c.probe_pending, c.session_active = previous_pid, False, session_active
    c.ended = 0
    c.sunshine_main_pid = lambda: current_pid
    c.session_end = lambda: setattr(c, "ended", c.ended + 1)
    return c


class WatchSunshineTest(unittest.TestCase):
    def test_restart_during_a_session_ends_it(self):
        # No undo prep-cmd runs when Sunshine dies, so the phantom would stay (and block lid suspend).
        c = make(100, 200, session_active=True)
        c.watch_sunshine()
        self.assertEqual(c.ended, 1)

    def test_stop_during_a_session_ends_it(self):
        c = make(100, 0, session_active=True)
        c.watch_sunshine()
        self.assertEqual(c.ended, 1)

    def test_new_start_needs_a_probe_again(self):
        c = make(100, 200, session_active=False)
        c.watch_sunshine()
        self.assertTrue(c.probe_pending)
        self.assertEqual(c.ended, 0)

    def test_first_look_after_controller_start_keeps_the_session(self):
        c = make(None, 100, session_active=True)
        c.watch_sunshine()
        self.assertEqual(c.ended, 0)

    def test_same_pid_changes_nothing(self):
        c = make(100, 100, session_active=True)
        c.watch_sunshine()
        self.assertEqual((c.ended, c.probe_pending), (0, False))


class RecordingBus:
    def __init__(self):
        self.methods = []

    def call_sync(self, _name, _path, _interface, method, *_rest):
        self.methods.append(method)


def lockable(locked):
    """A controller whose lock state is `locked` (a bool, or an exception to raise)."""
    c = controller.Controller.__new__(controller.Controller)
    c.bus = RecordingBus()

    def state():
        if isinstance(locked, Exception):
            raise locked
        return locked
    c.locked = state
    return c


class LockTest(unittest.TestCase):
    def test_already_locked_session_is_not_locked_again(self):
        # GNOME's Lock replies only when the lock screen appears; on a locked session it never does,
        # so the call hung for its 5 s timeout and delayed the unplug (seen 2026-10-09 13:30:42).
        c = lockable(True)
        c.lock()
        self.assertNotIn("Lock", c.bus.methods)

    def test_unlocked_session_gets_locked(self):
        c = lockable(False)
        c.lock()
        self.assertIn("Lock", c.bus.methods)

    def test_unknown_lock_state_still_locks(self):
        c = lockable(GLib.Error("logind unreachable"))
        c.lock()
        self.assertIn("Lock", c.bus.methods)


class LeftoverSessionTest(unittest.TestCase):
    def start(self, phantom_forced):
        c = controller.Controller.__new__(controller.Controller)
        c.calls = []
        c.lock_if_ipad_only = lambda: c.calls.append("lock")
        c.close_app_async = lambda: c.calls.append("close app")
        with mock.patch.object(controller, "phantom_forced", return_value=phantom_forced):
            c.end_leftover_session()
        return c.calls

    def test_phantom_at_start_ends_the_session_it_belonged_to(self):
        # A controller killed mid-session left Sunshine streaming from a vanished screen (frozen iPad,
        # 30 capture errors/s) and its successor unplugged without locking (2026-10-09 13:42).
        self.assertEqual(self.start(phantom_forced=True), ["lock", "close app"])

    def test_clean_start_touches_nothing(self):
        self.assertEqual(self.start(phantom_forced=False), [])


class StopTest(unittest.TestCase):
    def stop(self, phantom_forced):
        c = controller.Controller.__new__(controller.Controller)
        c.calls = []
        c.lock_if_ipad_only = lambda: c.calls.append("lock")
        c.helper = lambda action: c.calls.append(f"helper {action}") or True
        c.loop = mock.Mock(quit=lambda: c.calls.append("quit"))
        with mock.patch.object(controller, "phantom_forced", return_value=phantom_forced), \
                mock.patch.object(controller.sunshine_api, "close_app",
                                  side_effect=lambda *a, **k: c.calls.append("close app") or True):
            c.stop()
        return c.calls

    def test_stop_with_the_phantom_plugged_ends_the_session_itself(self):
        # Logout (PartOf=graphical-session.target) or `systemctl stop` sends SIGTERM and no successor may come
        # to clean up, so the forced phantom stayed and blocked lid suspend on battery (review 2026-10-10).
        self.assertEqual(self.stop(phantom_forced=True), ["lock", "close app", "helper off", "quit"])

    def test_stop_without_a_phantom_just_quits(self):
        self.assertEqual(self.stop(phantom_forced=False), ["quit"])


class SessionEndLockTest(unittest.TestCase):
    def locks(self, logical=None, error=None):
        c = controller.Controller.__new__(controller.Controller)
        c.calls = []
        c.lock = lambda: c.calls.append("lock")

        def current():
            if error:
                raise error
            return state([DP_MON, PH_MON], logical)
        c.state = current
        c.lock_if_ipad_only()
        return c.calls == ["lock"]

    def test_ipad_as_the_only_screen_locks(self):
        # The desktop is about to reappear on real screens nobody watched during the stream.
        self.assertTrue(self.locks([(0, 0, 2.0, 0, True, [PH], {})]))

    def test_real_screen_lit_alongside_the_ipad_does_not_lock(self):
        # Extend: the unlocked desktop was on the real screen all along, and unplugging a real monitor never
        # locks either (user 2026-10-10).
        self.assertFalse(self.locks([DP_LOGICAL, (597, 1440, 2.0, 0, False, [PH], {})]))

    def test_unknown_layout_locks(self):
        self.assertTrue(self.locks(error=GLib.Error("Mutter unreachable")))

    def test_session_end_applies_the_rule_before_the_unplug(self):
        c = controller.Controller.__new__(controller.Controller)
        c.calls = []
        c.lock = lambda: c.calls.append("lock")
        c.lock_if_ipad_only = lambda: c.calls.append("lock if iPad only")
        c.evaluate = lambda: c.calls.append("evaluate")
        c.session_end()
        self.assertEqual(c.calls, ["lock if iPad only", "evaluate"])


class PlugTest(unittest.TestCase):
    def test_plug_keeps_the_layout_saved_before_it(self):
        # Right after the plug Mutter shows its default arrangement for the new monitor set; saving that
        # replaced the user's layout, which the unplug then "restored" (review 2026-10-10).
        before = state([DP_MON, EDP_ON], USER_LOGICAL)
        after = state([DP_MON, EDP_ON, PH_MON], MUTTER_HOTPLUG_DEFAULT)
        c = controller.Controller.__new__(controller.Controller)
        c.session_active, c.saved_physical, c.bus = True, [], None
        states = [before]
        c.state = lambda: states.pop(0) if states else after
        c.helper = lambda action: True
        c.wait_for = lambda predicate, timeout=8.0: after
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(controller, "SAVED", Path(tmp) / "saved.json"), \
                mock.patch.object(controller, "read_mode", return_value="primary"), \
                mock.patch.object(controller.display, "apply"):
            c.reconcile(lid=False, relayout=True)
        self.assertEqual(c.saved_physical, USER_LAYOUT)


class SessionEndReplyTest(unittest.TestCase):
    def test_session_end_replies_before_the_slow_cleanup(self):
        # Sunshine waits for this undo inside proc_t::terminate() while it already reports the host as free;
        # a launch in that window runs terminate() again on another thread, which crashed Sunshine
        # (2026-10-09 14:58:39 SIGSEGV) or wedged it (15:44, every launch "Property tree is empty").
        ours, sunshine = socket.socketpair()
        self.addCleanup(sunshine.close)
        sunshine.sendall(b"session-end\n")
        sunshine.setblocking(False)
        c = controller.Controller.__new__(controller.Controller)
        c.server = mock.Mock(accept=lambda: (ours, None))
        replies_seen = []

        def session_end():
            try:
                replies_seen.append(sunshine.recv(64))
            except BlockingIOError:
                replies_seen.append(None)
            return "ended"
        c.session_end = session_end
        c.on_client(None, None)
        self.assertEqual(replies_seen, [b"ended\n"])


if __name__ == "__main__":
    unittest.main()
