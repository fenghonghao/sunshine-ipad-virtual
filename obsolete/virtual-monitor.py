#!/usr/bin/python3
"""Temporary Mutter virtual output. No persistent monitor or boot configuration."""
import json
import os
from pathlib import Path
import signal
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst

WIDTH, HEIGHT, FPS = 2732, 2048, 30
SERVICE = "org.gnome.Mutter.ScreenCast"
SESSION_IFACE = SERVICE + ".Session"
RUNTIME = Path(os.environ["XDG_RUNTIME_DIR"]) / "sunshine-ipad-virtual.json"


class VirtualMonitor:
    def __init__(self):
        Gst.init(None)
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.loop = GLib.MainLoop()
        self.session = None
        self.pipeline = None
        self.failed = False
        self.stopping = False
        self.ready = False
        self.node_id = None
        self.previous = self.display_state()
        self.previous_connectors = {m[0][0] for m in self.previous[1]}
        self.previous_layout = self.previous[2]
        self.subscriptions = []

    def call(self, path, interface, method, params=None):
        return self.bus.call_sync(SERVICE, path, interface, method, params, None,
                                  Gio.DBusCallFlags.NONE, 5000, None)

    def display_state(self):
        return self.bus.call_sync(
            "org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
            "org.gnome.Mutter.DisplayConfig", "GetCurrentState", None, None,
            Gio.DBusCallFlags.NONE, 5000, None).unpack()

    def stop(self, reason, failure=False):
        if self.stopping:
            return False
        self.stopping = True
        self.failed = self.failed or failure
        print(reason, flush=True)
        self.loop.quit()
        return False

    def on_message(self, bus, message):
        if message.type == Gst.MessageType.ERROR:
            error, detail = message.parse_error()
            self.stop(f"GStreamer error: {error}; {detail}", True)
        elif message.type == Gst.MessageType.EOS:
            self.stop("PipeWire stream ended", True)

    def on_node(self, connection, sender, path, interface, name, parameters):
        if self.pipeline is not None:
            return
        try:
            self.node_id = parameters.unpack()[0]
            self.pipeline = Gst.parse_launch(
                f'pipewiresrc path={self.node_id} do-timestamp=true ! '
                f'video/x-raw,width={WIDTH},height={HEIGHT},max-framerate={FPS}/1 ! '
                'fakesink sync=false enable-last-sample=false')
            bus = self.pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self.on_message)
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("PipeWire pipeline refused PLAYING state")
            print(f"PipeWire node {self.node_id} attached", flush=True)
        except Exception as error:
            self.stop(f"Virtual output initialization failed: {error}", True)

    def check_ready(self):
        if self.stopping:
            return False
        state = self.display_state()
        for spec, modes, props in state[1]:
            if spec[0] in self.previous_connectors:
                continue
            current = [m for m in modes if m[6].get("is-current")]
            if not any(m[1:3] == (WIDTH, HEIGHT) for m in current):
                continue
            # Do not save any display layout: Mutter adds/removes this output temporarily.
            data = {"pid": os.getpid(), "connector": spec[0],
                    "width": WIDTH, "height": HEIGHT, "fps": FPS,
                    "pipewire_node": self.node_id}
            RUNTIME.write_text(json.dumps(data, indent=2) + "\n")
            self.ready = True
            print("READY " + json.dumps(data), flush=True)
            return False
        return True

    def deadline(self):
        if not self.ready:
            return self.stop("Virtual output did not become ready within 20 seconds", True)
        return False

    def run(self):
        try:
            self.session = self.call("/org/gnome/Mutter/ScreenCast", SERVICE,
                "CreateSession", GLib.Variant("(a{sv})", ({},))).unpack()[0]
            stream = self.call(self.session, SESSION_IFACE, "RecordVirtual",
                GLib.Variant("(a{sv})", ({
                    "is-platform": GLib.Variant("b", True),
                    "cursor-mode": GLib.Variant("u", 1),
                },))).unpack()[0]
            self.subscriptions.append(self.bus.signal_subscribe(
                SERVICE, SERVICE + ".Stream", "PipeWireStreamAdded", stream,
                None, Gio.DBusSignalFlags.NONE, self.on_node))
            self.subscriptions.append(self.bus.signal_subscribe(
                SERVICE, SESSION_IFACE, "Closed", self.session, None,
                Gio.DBusSignalFlags.NONE,
                lambda *args: self.stop("Mutter closed the virtual monitor session", True)))
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM,
                                 lambda: self.stop("Requested shutdown"))
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT,
                                 lambda: self.stop("Requested shutdown"))
            self.call(self.session, SESSION_IFACE, "Start")
            GLib.timeout_add(250, self.check_ready)
            GLib.timeout_add_seconds(20, self.deadline)
            self.loop.run()
        finally:
            self.stopping = True
            if self.pipeline is not None:
                self.pipeline.set_state(Gst.State.NULL)
            if self.session:
                try:
                    self.call(self.session, SESSION_IFACE, "Stop")
                except GLib.Error as error:
                    print(f"Session already closed or Stop failed: {error}", flush=True)
            for subscription in self.subscriptions:
                self.bus.signal_unsubscribe(subscription)
            if RUNTIME.exists():
                data = json.loads(RUNTIME.read_text())
                if data.get("pid") == os.getpid():
                    RUNTIME.unlink()
        return 1 if self.failed else 0


if __name__ == "__main__":
    sys.exit(VirtualMonitor().run())
