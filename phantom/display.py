"""Mutter DisplayConfig helpers for the phantom iPad display.

Pure functions take the unpacked GetCurrentState tuple, so layouts are unit-tested
with fixtures; get_state/apply/wake talk to Mutter.
"""
from gi.repository import Gio, GLib

IFACE = "org.gnome.Mutter.DisplayConfig"
PATH = "/org/gnome/Mutter/DisplayConfig"
PHANTOM_VENDOR, PHANTOM_PRODUCT = "SIV", "iPad Virtual"
PHANTOM_SCALE = 2.0


def is_phantom(spec):
    return spec[1] == PHANTOM_VENDOR and spec[2] == PHANTOM_PRODUCT


def _monitor(state, connector):
    return next(((spec, modes, props) for spec, modes, props in state[1] if spec[0] == connector), None)


def _mode(state, connector, mode_id):
    monitor = _monitor(state, connector)
    return next((m for m in monitor[1] if m[0] == mode_id), None) if monitor else None


def phantom_monitor(state):
    """(connector, mode_id) of the phantom's preferred mode, or None when it is not plugged."""
    for spec, modes, _props in state[1]:
        if is_phantom(spec):
            preferred = next((m for m in modes if m[6].get("is-preferred")), modes[0])
            return spec[0], preferred[0]
    return None


def _usable(props, lid_closed):
    return not (props.get("is-builtin") and lid_closed)


def physical_available(state, lid_closed):
    """A real screen someone could look at: any external one, or the built-in panel with the lid open."""
    return any(not is_phantom(spec) and _usable(props, lid_closed) for spec, _modes, props in state[1])


def physical_logical(state):
    """Enabled logical monitors without the phantom, as layout tuples."""
    result = []
    for x, y, scale, transform, primary, specs, _props in state[2]:
        if any(is_phantom(spec) for spec in specs):
            continue
        monitors = []
        for spec in specs:
            current = next((m for m in _monitor(state, spec[0])[1] if m[6].get("is-current")), None)
            if current is None:
                break
            monitors.append((spec[0], current[0], {}))
        else:
            result.append((x, y, scale, transform, primary, monitors))
    return result


def _size(state, layout):
    x, y, scale, transform, primary, monitors = layout
    found = _mode(state, monitors[0][0], monitors[0][1])
    w, h = found[1], found[2]
    if transform % 2:  # 90° / 270° rotations swap the axes
        w, h = h, w
    if state[3].get("layout-mode", 1) == 1:  # logical layout mode: sizes are divided by scale
        w, h = round(w / scale), round(h / scale)
    return w, h


def _usable_saved(state, saved, lid_closed):
    """Snapshot entries whose connectors and modes still exist and can be lit."""
    keep = []
    for layout in saved:
        monitors = layout[5]
        if all(_mode(state, c, m) is not None and _usable(_monitor(state, c)[2], lid_closed)
               for c, m, _ in monitors):
            keep.append(layout)
    if keep and not any(layout[4] for layout in keep):
        keep[0] = (*keep[0][:4], True, keep[0][5])
    return keep


def _default_physical(state, lid_closed):
    """One real screen at its preferred mode; external screens before the built-in panel."""
    candidates = [(spec, modes, props) for spec, modes, props in state[1]
                  if not is_phantom(spec) and modes and _usable(props, lid_closed)]
    if not candidates:
        return []
    candidates.sort(key=lambda c: bool(c[2].get("is-builtin")))
    spec, modes, _props = candidates[0]
    preferred = next((m for m in modes if m[6].get("is-preferred")), modes[0])
    return [(0, 0, float(preferred[4]), 0, True, [(spec[0], preferred[0], {})])]


def saved_matches(state, lid_closed, saved):
    """The snapshot names exactly the real screens that can be lit now, all with existing modes."""
    usable = {spec[0] for spec, _modes, props in state[1] if not is_phantom(spec) and _usable(props, lid_closed)}
    named = {connector for layout in saved for connector, _mode, _props in layout[5]}
    return bool(saved) and named == usable and len(_usable_saved(state, saved, lid_closed)) == len(saved)


def restore_layout(state, lid_closed, saved):
    """Real screens to light after the phantom left nothing enabled."""
    return _usable_saved(state, saved, lid_closed) or _default_physical(state, lid_closed)


def desired_layout(state, mode, lid_closed, saved):
    """Logical monitors to apply while the phantom is plugged."""
    connector, mode_id = phantom_monitor(state)
    phantom = [(connector, mode_id, {})]
    physical = []
    if mode == "extend" and physical_available(state, lid_closed):
        if saved_matches(state, lid_closed, saved):  # the user's arrangement, not Mutter's hotplug default
            physical = restore_layout(state, lid_closed, saved)
        else:
            physical = physical_logical(state) or restore_layout(state, lid_closed, saved)
    if not physical:
        return [(0, 0, PHANTOM_SCALE, 0, True, phantom)]
    anchor = max(physical, key=lambda layout: layout[1] + _size(state, layout)[1])
    anchor_w, anchor_h = _size(state, anchor)
    phantom_w = _size(state, (0, 0, PHANTOM_SCALE, 0, False, phantom))[0]
    x = anchor[0] + max((anchor_w - phantom_w) // 2, 0)
    return physical + [(x, anchor[1] + anchor_h, PHANTOM_SCALE, 0, False, phantom)]


def get_state(bus):
    return bus.call_sync(IFACE, PATH, IFACE, "GetCurrentState", None, None,
                         Gio.DBusCallFlags.NONE, 5000, None).unpack()


def apply(bus, state, layout):
    """Method 1 = temporary: never written to ~/.config/monitors.xml."""
    props = {"layout-mode": GLib.Variant("u", state[3].get("layout-mode", 1))}
    args = GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})", (state[0], 1, layout, props))
    bus.call_sync(IFACE, PATH, IFACE, "ApplyMonitorsConfig", args, None,
                  Gio.DBusCallFlags.NONE, 10000, None)


def power_save_mode(bus):
    """0 = screens on; anything else = blanked."""
    return bus.call_sync(IFACE, PATH, "org.freedesktop.DBus.Properties", "Get",
                         GLib.Variant("(ss)", (IFACE, "PowerSaveMode")),
                         None, Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]


def set_power_save(bus, mode):
    bus.call_sync(IFACE, PATH, "org.freedesktop.DBus.Properties", "Set",
                  GLib.Variant("(ssv)", (IFACE, "PowerSaveMode", GLib.Variant("i", mode))),
                  None, Gio.DBusCallFlags.NONE, 5000, None)


def wake(bus):
    """Turn every screen on (DPMS) without simulated input."""
    set_power_save(bus, 0)
