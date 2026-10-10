import unittest

from contact import contact_action, remote_peers

HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"


def line(local, remote, state="01"):
    return f"   0: {local} {remote} {state} 00000000:00000000 00:00000000 00000000  1000        0 1 1"


class RemotePeersTest(unittest.TestCase):
    def test_established_lan_peer_on_https_port(self):
        # 192.168.0.5:47984 <- 192.168.0.9:54321 (little-endian hex per 32-bit word)
        text = "\n".join([HEADER, line("0500A8C0:BB70", "0900A8C0:D431")])
        self.assertEqual(remote_peers(text), {"0900A8C0:D431"})

    def test_http_port_counts_too(self):
        text = "\n".join([HEADER, line("0500A8C0:BB75", "0900A8C0:D431")])
        self.assertEqual(len(remote_peers(text)), 1)

    def test_listening_socket_and_other_ports_are_ignored(self):
        text = "\n".join([HEADER, line("00000000:BB70", "00000000:0000", "0A"),
                          line("0500A8C0:BB76", "0900A8C0:D431")])  # 47990 = web UI
        self.assertEqual(remote_peers(text), set())

    def test_loopback_peers_are_ignored(self):
        v6_loopback = "00000000000000000000000001000000"
        v4_mapped_loopback = "0000000000000000FFFF00000100007F"
        text = "\n".join([HEADER, line("0100007F:BB70", "0100007F:D431"),
                          HEADER, line(v6_loopback + ":BB70", v6_loopback + ":D431"),
                          line(v4_mapped_loopback + ":BB70", v4_mapped_loopback + ":D432")])
        self.assertEqual(remote_peers(text), set())

    def test_time_wait_after_a_finished_request_counts(self):
        # Moonlight's host-list polls last ~0.1 s; Sunshine closes first, so each one leaves a
        # server-side TIME_WAIT entry for ~60 s (seen 2026-10-09 with the iPad over Tailscale).
        text = "\n".join([HEADER, line("0500A8C0:BB75", "0900A8C0:D431", "06")])
        self.assertEqual(remote_peers(text), {"0900A8C0:D431"})

    def test_handshake_and_closing_states_count(self):
        text = "\n".join([HEADER, line("0500A8C0:BB70", "0900A8C0:D431", "03"),   # SYN_RECV
                          line("0500A8C0:BB70", "0900A8C0:D432", "09")])           # LAST_ACK
        self.assertEqual(len(remote_peers(text)), 2)

    def test_ipv6_peer_after_second_header(self):
        peer = "0000000000000000FFFF00000900A8C0:D431"  # ::ffff:192.168.0.9
        text = "\n".join([HEADER, HEADER, line("0000000000000000FFFF00000500A8C0:BB70", peer)])
        self.assertEqual(remote_peers(text), {peer})


class ContactActionTest(unittest.TestCase):
    def act(self, contact=False, blanked=False, locked=True, session=False, woke_at=None,
            last_contact=0.0, now=100.0, probe_pending=True):
        return contact_action(contact, blanked, locked, session, woke_at, last_contact, now,
                              probe_pending=probe_pending)

    def test_contact_while_blanked_wakes(self):
        self.assertEqual(self.act(contact=True, blanked=True), "wake")

    def test_no_wake_once_sunshine_has_probed(self):
        # Sunshine re-probes encoders (and needs a lit screen) only before its first launch after
        # each start (video.cpp:3275 with a fixed adapter_name); later launches work while blanked.
        self.assertIsNone(self.act(contact=True, blanked=True, probe_pending=False))

    def test_contact_while_lit_does_nothing(self):
        self.assertIsNone(self.act(contact=True, blanked=False))

    def test_no_wake_during_a_session(self):
        self.assertIsNone(self.act(contact=True, blanked=True, session=True))

    def test_reblank_after_linger_while_still_locked(self):
        self.assertEqual(self.act(woke_at=10.0, last_contact=40.0, now=100.0), "reblank")
        self.assertIsNone(self.act(woke_at=10.0, last_contact=41.0, now=100.0))

    def test_no_reblank_while_moonlight_keeps_polling(self):
        self.assertIsNone(self.act(contact=True, woke_at=10.0, last_contact=100.0, now=100.0))

    def test_unlock_or_session_hands_blanking_back_to_gnome(self):
        self.assertEqual(self.act(woke_at=10.0, locked=False), "forget")
        self.assertEqual(self.act(woke_at=10.0, session=True), "forget")

    def test_rewake_if_gnome_blanked_again_while_polling(self):
        self.assertEqual(self.act(contact=True, blanked=True, woke_at=10.0, last_contact=100.0), "wake")


if __name__ == "__main__":
    unittest.main()
