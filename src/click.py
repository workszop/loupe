"""Synthetic mouse click via /dev/uinput.

A Wayland client cannot click through to another application, but it can create
a virtual input device (if it has access to /dev/uinput — the logged-in user
usually does, via a systemd-logind seat ACL) and emit a button event, which the
compositor delivers to whatever window is under the pointer.

The loupe leaves the real pointer exactly where the user aimed (the magnifier is
centered on the cursor, so the crosshair at the lens center IS the click point).
So clicking needs no pointer movement — just a press/release at the current
position, after the loupe window has hidden itself.

VirtualPointer is created once at startup so the compositor has time to register
the device before the first click (a device created and used in the same
instant tends to drop its first events).
"""
from __future__ import annotations

import time


class VirtualPointer:
    """A uinput virtual pointer that can emit a click at the current position.

    Construction raises (ImportError / OSError / PermissionError) if evdev is
    missing or /dev/uinput is not writable; callers should treat clicking as
    unavailable in that case.
    """

    def __init__(self) -> None:
        from evdev import UInput
        from evdev import ecodes as e

        self._e = e
        self._ui = UInput(
            {e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT], e.EV_REL: [e.REL_X, e.REL_Y]},
            name="loupe-virtual-pointer",
        )

    def click(self, button: str = "left") -> None:
        e = self._e
        code = e.BTN_RIGHT if button == "right" else e.BTN_LEFT
        self._ui.write(e.EV_KEY, code, 1)
        self._ui.syn()
        time.sleep(0.02)
        self._ui.write(e.EV_KEY, code, 0)
        self._ui.syn()

    def close(self) -> None:
        try:
            self._ui.close()
        except Exception:
            pass
