"""loupe entry point: a freeze-frame screen magnifier.

Flow: acquire the single-instance pidfile (or toggle off a running instance),
grab a silent full-screen screenshot, open a fullscreen Gtk.Application window
showing that frozen image, and draw a magnifier lens centered on the cursor.
Esc / click / Ctrl+Q / a second Super+Z all dismiss it.

Usage: loupe.py
On COSMIC, bind Super+Z to run this script; a second Super+Z toggles it off.
"""
from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")

from gi.repository import Gio, GLib, Gtk  # noqa: E402

# NOTE: `from X import name` (not `import X`) deliberately — after
# tools/build.py bundles the src modules into one flat loupe.py, there is no
# separate `capture`/`click`/`ui`/`lifecycle` namespace to qualify against.
from capture import grab_screenshot  # noqa: E402
from click import VirtualPointer  # noqa: E402
from lifecycle import (  # noqa: E402
    acquire_pidfile_or_toggle,
    fail,
    install_signal_handlers,
    release_pidfile,
)
from ui import LoupeWindow  # noqa: E402

APP_ID = "dev.andrzey.loupe"

# Delay after hiding the loupe window before firing the synthetic click, so the
# compositor re-routes pointer focus to the window underneath first.
CLICK_THROUGH_DELAY_MS = 130


def main(argv: list[str]) -> int:
    if not acquire_pidfile_or_toggle():
        return 0

    try:
        texture = grab_screenshot()
    except Exception as exc:  # noqa: BLE001 — surface any capture failure cleanly
        fail("screen capture failed", hint=str(exc))
        release_pidfile()
        return 1

    # Best-effort virtual pointer for click-through. Created up front so the
    # compositor registers it before the first click; if uinput/evdev is
    # unavailable, clicking falls back to plain dismiss and reading still works.
    try:
        pointer = VirtualPointer()
    except Exception as exc:  # noqa: BLE001
        pointer = None
        print(f"loupe: click-through unavailable ({exc}); clicks will dismiss.",
              file=sys.stderr)

    app = Gtk.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
    state = {"cleaned_up": False}

    def cleanup():
        if state["cleaned_up"]:
            return
        state["cleaned_up"] = True
        if pointer is not None:
            pointer.close()
        release_pidfile()
        app.quit()

    def on_activate(a):
        install_signal_handlers(cleanup)

        window = LoupeWindow(
            application=a,
            texture=texture,
            on_quit=cleanup,
            on_click_through=(None if pointer is None else lambda: click_through(window)),
        )
        window.present()

    def click_through(window):
        # Hide the loupe so the synthetic click lands on the app underneath,
        # let pointer focus settle, then click at the (unchanged) cursor spot.
        window.set_visible(False)

        def fire():
            try:
                pointer.click("left")
            except Exception as exc:  # noqa: BLE001
                fail("click failed", hint=str(exc))
            cleanup()
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(CLICK_THROUGH_DELAY_MS, fire)

    app.connect("activate", on_activate)
    return app.run(None)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
