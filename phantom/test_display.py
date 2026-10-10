import unittest

import display

DP = ("DP-1", "XMI", "Mi Monitor", "5877500049365")
EDP = ("eDP-1", "CSW", "MNG007ZA1-4", "0x00006008")
PH = ("HDMI-1", "SIV", "iPad Virtual", "0x00000001")


def mode(mid, w, h, current=False, preferred=False, scale=1.0):
    return (mid, w, h, 60.0, scale, [1.0, 1.5, 2.0], {"is-current": current, "is-preferred": preferred})


def state(monitors, logical):
    return (7, monitors, logical, {"layout-mode": 1})


DP_MON = (DP, [mode("3840x2160@60.000", 3840, 2160, current=True, preferred=True, scale=1.5)], {})
DP_OFF = (DP, [mode("3840x2160@60.000", 3840, 2160, preferred=True, scale=1.5)], {})
EDP_MON = (EDP, [mode("3200x2000@165.000", 3200, 2000, preferred=True, scale=2.0)], {"is-builtin": True})
PH_MON = (PH, [mode("2732x2048@59.999", 2732, 2048, preferred=True, scale=2.0),
               mode("2732x2048@56.999", 2732, 2048)], {})
DP_LOGICAL = (0, 0, 1.5, 0, True, [DP], {})
# Lid open with DP-1: the user's arrangement, and what Mutter applies once the phantom appears.
EDP_ON = (EDP, [mode("3200x2000@165.000", 3200, 2000, current=True, preferred=True, scale=2.0)], {"is-builtin": True})
USER_LOGICAL = [(0, 0, 2.0, 0, False, [EDP], {}), (1600, 0, 1.5, 0, True, [DP], {})]
USER_LAYOUT = [(0, 0, 2.0, 0, False, [("eDP-1", "3200x2000@165.000", {})]),
               (1600, 0, 1.5, 0, True, [("DP-1", "3840x2160@60.000", {})])]
MUTTER_HOTPLUG_DEFAULT = [(0, 0, 1.5, 0, True, [DP], {}), (2560, 0, 2.0, 0, False, [EDP], {}),
                          (4160, 0, 2.0, 0, False, [PH], {})]


class DisplayTest(unittest.TestCase):
    def test_phantom_detected_by_vendor_and_product(self):
        self.assertEqual(display.phantom_monitor(state([DP_MON, PH_MON], [DP_LOGICAL])), ("HDMI-1", "2732x2048@59.999"))
        self.assertIsNone(display.phantom_monitor(state([DP_MON], [DP_LOGICAL])))

    def test_lid_closed_builtin_is_not_a_real_screen(self):
        self.assertFalse(display.physical_available(state([EDP_MON, PH_MON], []), lid_closed=True))
        self.assertTrue(display.physical_available(state([EDP_MON], []), lid_closed=False))
        self.assertTrue(display.physical_available(state([DP_OFF, PH_MON], []), lid_closed=True))

    def test_primary_mode_leaves_only_the_phantom(self):
        st = state([DP_MON, PH_MON], [DP_LOGICAL])
        self.assertEqual(display.desired_layout(st, "primary", True, []),
                         [(0, 0, 2.0, 0, True, [("HDMI-1", "2732x2048@59.999", {})])])

    def test_extend_mode_centres_phantom_below_external(self):
        st = state([DP_MON, PH_MON], [DP_LOGICAL])
        self.assertEqual(display.desired_layout(st, "extend", True, []), [
            (0, 0, 1.5, 0, True, [("DP-1", "3840x2160@60.000", {})]),
            (597, 1440, 2.0, 0, False, [("HDMI-1", "2732x2048@59.999", {})]),
        ])

    def test_extend_mode_from_primary_uses_saved_physical(self):
        # Real screens are disabled (primary mode was active): rebuild from the snapshot.
        st = state([DP_OFF, PH_MON], [(0, 0, 2.0, 0, True, [PH], {})])
        saved = [(0, 0, 1.5, 0, True, [("DP-1", "3840x2160@60.000", {})])]
        self.assertEqual(display.desired_layout(st, "extend", True, saved)[1][:2], (597, 1440))

    def test_extend_without_real_screen_falls_back_to_primary(self):
        st = state([EDP_MON, PH_MON], [])
        self.assertEqual(len(display.desired_layout(st, "extend", True, [])), 1)

    def test_extend_mode_keeps_the_saved_arrangement_over_mutters_hotplug_default(self):
        # On hotplug Mutter applies its default arrangement to a monitor set it has no stored config for;
        # the snapshot taken before the plug is the user's own layout (review 2026-10-10).
        st = state([DP_MON, EDP_ON, PH_MON], MUTTER_HOTPLUG_DEFAULT)
        layout = display.desired_layout(st, "extend", False, USER_LAYOUT)
        self.assertEqual(layout[:2], USER_LAYOUT)
        self.assertEqual(layout[2][:2], (1600 + 597, 1440))  # centred below DP-1, the bottom-most screen

    def test_restore_layout_prefers_saved_physical(self):
        st = state([DP_OFF], [])
        saved = [(0, 0, 1.5, 0, True, [("DP-1", "3840x2160@60.000", {})])]
        self.assertEqual(display.restore_layout(st, True, saved), saved)

    def test_restore_layout_default_external_over_lid_closed_builtin(self):
        st = state([EDP_MON, DP_OFF], [])
        self.assertEqual(display.restore_layout(st, True, []),
                         [(0, 0, 1.5, 0, True, [("DP-1", "3840x2160@60.000", {})])])

    def test_saved_matches_only_the_same_set_of_real_screens(self):
        saved = [(0, 0, 1.5, 0, True, [("DP-1", "3840x2160@60.000", {})])]
        both = state([EDP_MON, DP_OFF], [])
        self.assertFalse(display.saved_matches(both, False, saved))  # lid open: eDP-1 would be left dark
        self.assertTrue(display.saved_matches(both, True, saved))    # lid closed: DP-1 is the only one
        self.assertFalse(display.saved_matches(both, True, []))

    def test_saved_physical_with_vanished_connector_is_ignored(self):
        st = state([EDP_MON], [])
        saved = [(0, 0, 1.5, 0, True, [("DP-1", "3840x2160@60.000", {})])]
        self.assertEqual(display.restore_layout(st, False, saved),
                         [(0, 0, 2.0, 0, True, [("eDP-1", "3200x2000@165.000", {})])])


if __name__ == "__main__":
    unittest.main()
