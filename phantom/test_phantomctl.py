import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

PHANTOMCTL = Path(__file__).resolve().parent.parent / "bin" / "phantomctl"


class SessionEndTest(unittest.TestCase):
    def test_session_end_does_not_wait_for_a_busy_controller(self):
        # Sunshine waits for this undo inside proc_t::terminate() while reporting the host as free. The
        # controller's loop can be busy for seconds (unplug + wait for Mutter), and a launch in that window
        # ran a second terminate() concurrently: SIGSEGV again at 2026-10-09 16:56:09.
        with tempfile.TemporaryDirectory() as runtime:
            busy = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # listens, never accepts or replies
            self.addCleanup(busy.close)
            busy.bind(os.path.join(runtime, "sunshine-phantom.sock"))
            busy.listen(4)
            try:
                result = subprocess.run([str(PHANTOMCTL), "session-end"], env={**os.environ, "XDG_RUNTIME_DIR": runtime},
                                        capture_output=True, text=True, timeout=2)
            except subprocess.TimeoutExpired:
                self.fail("session-end waited for the controller's reply")
            self.assertEqual(result.returncode, 0)
            connection, _ = busy.accept()  # the controller still gets the command once it is free
            with connection:
                self.assertEqual(connection.recv(64), b"session-end\n")


if __name__ == "__main__":
    unittest.main()
