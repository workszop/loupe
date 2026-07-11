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

from gi.repository import Gio, Gtk  # noqa: E402

# NOTE: `from X import name` (not `import X`) deliberately — after
# tools/build.py bundles the src modules into one flat loupe.py, there is no
# separate `capture`/`ui`/`lifecycle` namespace to qualify against.
from capture import grab_screenshot  # noqa: E402
from lifecycle import (  # noqa: E402
    acquire_pidfile_or_toggle,
    fail,
    install_signal_handlers,
    release_pidfile,
)
from ui import LoupeWindow  # noqa: E402

APP_ID = "dev.andrzey.loupe"


def main(argv: list[str]) -> int:
    if not acquire_pidfile_or_toggle():
        return 0

    try:
        texture = grab_screenshot()
    except Exception as exc:  # noqa: BLE001 — surface any capture failure cleanly
        fail("screen capture failed", hint=str(exc))
        release_pidfile()
        return 1

    app = Gtk.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
    state = {"cleaned_up": False}

    def cleanup():
        if state["cleaned_up"]:
            return
        state["cleaned_up"] = True
        release_pidfile()
        app.quit()

    def on_activate(a):
        install_signal_handlers(cleanup)
        window = LoupeWindow(application=a, texture=texture, on_quit=cleanup)
        window.present()

    app.connect("activate", on_activate)
    return app.run(None)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
