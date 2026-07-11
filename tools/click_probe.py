#!/usr/bin/env python3
"""Manual diagnostic: does a synthetic uinput click survive loupe's
hide-then-click-then-exit sequence on this compositor?

Opens two maximized windows in one app: the top one hides itself (like the
loupe does), a synthetic BTN_LEFT press/release fires 130ms later, and the
bottom window reports whether the click arrived. Prints CLICK-RECEIVED or
NO-CLICK; exit code 0/1. Briefly flashes on screen; clicks only its own
window.

Modes (how the uinput device is closed after the release write):
  --close-fast       immediately — loupe's original bug; expect NO-CLICK.
                     The compositor loses queued events when the device
                     vanishes before it reads them.
  --close-delayed    150ms later — the shipped fix (CLICK_SETTLE_MS);
                     expect CLICK-RECEIVED.
  (default)          when the app exits — baseline; expect CLICK-RECEIVED.

History: 2026-07-11, click-through "didn't click" on cosmic-comp. This probe
isolated the cause: --close-fast loses even the press event, while the same
sequence with the device kept open 150ms delivers reliably.
"""
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Gio  # noqa: E402
from evdev import UInput, ecodes as e  # noqa: E402

HIDE_TO_CLICK_MS = 130   # loupe's CLICK_THROUGH_DELAY_MS
SETTLE_MS = 150          # loupe's CLICK_SETTLE_MS

CLOSE_FAST = "--close-fast" in sys.argv
CLOSE_DELAYED = "--close-delayed" in sys.argv

ui = UInput(
    {e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT], e.EV_REL: [e.REL_X, e.REL_Y]},
    name="loupe-probe",
)

app = Gtk.Application(
    application_id="dev.andrzey.loupe.probe", flags=Gio.ApplicationFlags.NON_UNIQUE
)
outcome = {"code": 1}


def on_activate(a):
    detector = Gtk.ApplicationWindow(application=a)
    detector.set_decorated(False)
    detector.maximize()
    detector.set_child(Gtk.Label(label="loupe click probe: detector (bottom)"))

    click = Gtk.GestureClick.new()
    click.set_button(0)

    def on_pressed(gesture, _n, x, y):
        print(f"CLICK-RECEIVED on detector at ({x:.0f},{y:.0f})", flush=True)
        outcome["code"] = 0
        a.quit()

    click.connect("pressed", on_pressed)
    detector.add_controller(click)
    detector.present()

    cover = Gtk.ApplicationWindow(application=a)
    cover.set_decorated(False)
    cover.maximize()
    cover.set_child(Gtk.Label(label="loupe click probe: cover (top, will hide)"))

    def show_cover():
        cover.present()
        GLib.timeout_add(800, hide_cover)
        return GLib.SOURCE_REMOVE

    def hide_cover():
        cover.set_visible(False)
        GLib.timeout_add(HIDE_TO_CLICK_MS, press)
        return GLib.SOURCE_REMOVE

    def press():
        mode = "fast" if CLOSE_FAST else "delayed" if CLOSE_DELAYED else "on-exit"
        print(f"firing synthetic click (close mode: {mode})...", flush=True)
        ui.write(e.EV_KEY, e.BTN_LEFT, 1)
        ui.syn()
        GLib.timeout_add(20, release)
        return GLib.SOURCE_REMOVE

    def release():
        ui.write(e.EV_KEY, e.BTN_LEFT, 0)
        ui.syn()
        if CLOSE_FAST:
            ui.close()
        elif CLOSE_DELAYED:
            GLib.timeout_add(SETTLE_MS, close_device)
        return GLib.SOURCE_REMOVE

    def close_device():
        ui.close()
        return GLib.SOURCE_REMOVE

    def deadline():
        print("NO-CLICK: detector never saw the synthetic click", flush=True)
        a.quit()
        return GLib.SOURCE_REMOVE

    GLib.timeout_add(300, show_cover)
    GLib.timeout_add(3500, deadline)


app.connect("activate", on_activate)
app.run(None)
sys.exit(outcome["code"])
