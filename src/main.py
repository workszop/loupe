"""loupe entry point: a freeze-frame screen magnifier.

Flow: grab a silent full-screen screenshot, open a maximized Gtk.Application
window showing that frozen image, and draw a magnifier lens centered on the
cursor. Esc / click / Ctrl+Q / SIGTERM all dismiss it.

Usage: run via `loupe-toggle` (bind it to Super+Z on COSMIC), which starts
this as a transient systemd user unit named "loupe" — the unit name is the
single-instance lock, a second toggle stops the unit (SIGTERM), and stderr
lands in `journalctl --user -u loupe`.
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
from lifecycle import fail, install_signal_handlers  # noqa: E402
from ui import LoupeWindow  # noqa: E402

APP_ID = "dev.andrzey.loupe"

# Delay after hiding the loupe window before firing the synthetic click, so the
# compositor re-routes pointer focus to the window underneath first.
CLICK_THROUGH_DELAY_MS = 130

# Delay after the synthetic click before closing the uinput device and exiting.
# Closing (or exiting) immediately after the release write destroys the queued
# events before the compositor reads them and the click is silently lost —
# verified against cosmic-comp with tools/click_probe.py.
CLICK_SETTLE_MS = 150


def main(argv: list[str]) -> int:
    try:
        texture = grab_screenshot()
    except Exception as exc:  # noqa: BLE001 — surface any capture failure cleanly
        fail("screen capture failed", hint=str(exc))
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
    cleaned_up = False

    def cleanup():
        nonlocal cleaned_up
        if cleaned_up:
            return
        cleaned_up = True
        if pointer is not None:
            pointer.close()
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
            # Not cleanup() directly: the device must stay open until the
            # compositor has consumed the click events (see CLICK_SETTLE_MS).
            GLib.timeout_add(CLICK_SETTLE_MS, settle)
            return GLib.SOURCE_REMOVE

        def settle():
            cleanup()
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(CLICK_THROUGH_DELAY_MS, fire)

    app.connect("activate", on_activate)
    return app.run(None)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
