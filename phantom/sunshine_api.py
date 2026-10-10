"""Close the running Sunshine app over its local web API, so the app's undo prep-cmd runs."""
import base64
import ssl
import urllib.error
import urllib.request
from pathlib import Path

CREDENTIALS = Path.home() / ".config/sunshine-ipad-virtual/credentials"  # "user:password", mode 0600
CLOSE_URL = "https://localhost:47990/api/apps/close"
# The web UI serves Sunshine's own self-signed certificate: trust exactly that one.
SUNSHINE_CERT = Path.home() / ".config/sunshine/credentials/cacert.pem"


# Info/error lines logged when a stream ends. Only a client-side disconnect logs "CLIENT DISCONNECTED";
# sessions Sunshine stops itself (ping timeout, bad control packets) log one of the others instead.
# The line common to every path ("Session ended") is debug-level (stream.cpp 63d35f7).
SESSION_END_MARKERS = ("CLIENT DISCONNECTED", "Ping Timeout", "Failed to verify tag", "Bad packet type")


def is_disconnect(line):
    return any(marker in line for marker in SESSION_END_MARKERS)


def close_app(credentials=CREDENTIALS, url=CLOSE_URL, timeout=30, cafile=SUNSHINE_CERT):
    """True if Sunshine accepted the close; never raises.

    No Origin/Referer header is sent, so Sunshine skips the CSRF token
    (confighttp.cpp:827). closeApp runs the undo prep-cmd before replying,
    hence the generous timeout.
    """
    try:
        token = base64.b64encode(credentials.read_text().strip().encode()).decode()
        request = urllib.request.Request(url, data=b"", method="POST",
                                         headers={"Authorization": "Basic " + token})
        context = None
        if url.startswith("https:"):
            context = ssl.create_default_context(cafile=str(cafile))
            context.check_hostname = False  # CN is "Sunshine Gamestream Host"; the pinned cert is the check
        with urllib.request.urlopen(request, context=context, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (OSError, ValueError) as error:  # URLError/HTTPError are OSErrors
        if isinstance(error, urllib.error.HTTPError):
            error.close()  # an HTTPError carries the open response
        print(f"close_app failed: {error}", flush=True)
        return False
