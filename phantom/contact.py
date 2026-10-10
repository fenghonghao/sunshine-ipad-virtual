"""Wake blanked screens when Moonlight starts talking to Sunshine.

Before its first launch after each start, Sunshine probes encoders on a lit display before it
runs the connect prep-cmd (nvhttp.cpp:1393 before :1414; later launches skip the probe,
video.cpp:3275, because adapter_name is fixed). KMS sees no display while the screens are
blanked. Moonlight polls serverinfo on the nvhttp ports about every 2 s while its host list is
open, seconds before a launch, so a connection entry there is the earliest usable signal.
"""

SUNSHINE_PORTS = (47984, 47989)  # nvhttp HTTPS / HTTP
LINGER = 60.0                    # seconds without contact before re-blanking screens we woke


def _loopback(hex_addr):
    if len(hex_addr) == 8:  # IPv4, little-endian: 127.x.x.x ends in 7F
        return hex_addr.endswith("7F")
    return (hex_addr == "00000000000000000000000001000000"  # ::1
            or (hex_addr.startswith("0000000000000000FFFF0000") and hex_addr.endswith("7F")))


def remote_peers(proc_net_tcp, ports=SUNSHINE_PORTS):
    """Non-loopback peers with any connection entry on one of `ports` (/proc/net/tcp{,6} text).

    Moonlight's polls last ~0.1 s, but Sunshine closes first and keeps a TIME_WAIT entry for ~60 s,
    so counting every state except LISTEN catches each poll even with 1 s sampling.
    """
    peers = set()
    for line in proc_net_tcp.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[3] in ("0A", "st"):  # 0A = LISTEN; "st" = header line
            continue
        local_port = int(fields[1].rsplit(":", 1)[1], 16)
        if local_port in ports and not _loopback(fields[2].rsplit(":", 1)[0]):
            peers.add(fields[2])
    return peers


def contact_action(contact, blanked, locked, session_active, woke_at, last_contact, now, *, probe_pending,
                   linger=LINGER):
    """'wake', 'reblank' (screens we woke, nothing came of it), 'forget' (GNOME owns blanking again) or None."""
    if woke_at is None:
        return "wake" if contact and blanked and probe_pending and not session_active else None
    if session_active or not locked:
        return "forget"
    if contact and blanked:
        return "wake"
    if not contact and not blanked and now - last_contact >= linger:
        return "reblank"
    return None
