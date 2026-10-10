import base64
import http.server
import tempfile
import threading
import unittest
from pathlib import Path

import sunshine_api


class Handler(http.server.BaseHTTPRequestHandler):
    expected = "Basic " + base64.b64encode(b"sunshine:test-secret").decode()

    def do_POST(self):
        ok = self.path == "/api/apps/close" and self.headers.get("Authorization") == self.expected \
            and "Origin" not in self.headers and "Referer" not in self.headers
        self.send_response(200 if ok else 401)
        self.end_headers()
        self.wfile.write(b'{"status":true}' if ok else b"{}")

    def log_message(self, *args):
        pass


class CloseAppTest(unittest.TestCase):
    def setUp(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/apps/close"
        self.tmp = tempfile.TemporaryDirectory()
        self.running = True

    def tearDown(self):
        if self.running:  # shutdown() on a server that is not serving blocks forever
            self.server.shutdown()
            self.server.server_close()
        self.tmp.cleanup()

    def credentials(self, text):
        path = Path(self.tmp.name) / "credentials"
        path.write_text(text)
        return path

    def test_close_app_success(self):
        self.assertTrue(sunshine_api.close_app(self.credentials("sunshine:test-secret\n"), self.url, timeout=5))

    def test_close_app_wrong_password_returns_false(self):
        self.assertFalse(sunshine_api.close_app(self.credentials("sunshine:nope"), self.url, timeout=5))

    def test_close_app_missing_credentials_returns_false(self):
        self.assertFalse(sunshine_api.close_app(Path(self.tmp.name) / "absent", self.url, timeout=5))

    def test_close_app_server_down_returns_false(self):
        self.server.shutdown()
        self.server.server_close()
        self.running = False
        self.assertFalse(sunshine_api.close_app(self.credentials("sunshine:test-secret"), self.url, timeout=2))

    def test_close_app_failure_leaks_no_response(self):
        import gc
        import warnings
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            sunshine_api.close_app(self.credentials("sunshine:nope"), self.url, timeout=5)
            gc.collect()
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])

    def test_is_disconnect(self):
        self.assertTrue(sunshine_api.is_disconnect("[2026-10-08 16:37:19.874]: Info: CLIENT DISCONNECTED"))
        self.assertFalse(sunshine_api.is_disconnect("[2026-10-08 16:33:45.516]: Info: CLIENT CONNECTED"))

    def test_sessions_sunshine_stops_itself_count_as_disconnects(self):
        # These paths call session::stop and enet_peer_disconnect_now, which raises no ENet DISCONNECT event,
        # so "CLIENT DISCONNECTED" never appears (stream.cpp 63d35f7 :1225, :1280, :1291, :1331, :2122).
        for line in ("[2026-10-10 00:30:00.000]: Info: 192.0.2.10:51234: Ping Timeout",
                     "[2026-10-10 00:30:00.000]: Error: Initial Ping Timeout",
                     "[2026-10-10 00:30:00.000]: Error: Failed to verify tag",
                     "[2026-10-10 00:30:00.000]: Error: Bad packet type [IDX_ENCRYPTED] found"):
            self.assertTrue(sunshine_api.is_disconnect(line), line)


if __name__ == "__main__":
    unittest.main()
