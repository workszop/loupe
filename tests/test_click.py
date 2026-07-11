"""Test the VirtualPointer device can be created (uinput accessible).

This creates and destroys the virtual input device but deliberately does NOT
emit a click, so running the test never moves the pointer or clicks anything.
Skips cleanly if evdev is missing or /dev/uinput is not writable.
"""
import os

import pytest

from src.click import VirtualPointer

pytestmark = pytest.mark.skipif(
    not os.access("/dev/uinput", os.W_OK),
    reason="/dev/uinput not writable in this environment",
)


def test_virtual_pointer_creates_and_closes():
    try:
        import evdev  # noqa: F401
    except ImportError:
        pytest.skip("python-evdev not installed")

    # Constructing succeeds only if /dev/uinput is writable and the capability
    # set is valid — that is the whole capability the click-through relies on.
    # We do NOT emit a click here, so the pointer never moves and nothing is
    # clicked. evdev exposes the created node's own InputDevice; check its name.
    # Construction succeeds only if /dev/uinput is writable and the capability
    # set is valid — the whole capability the click-through relies on.
    vp = VirtualPointer()
    try:
        assert vp._ui.name == "loupe-virtual-pointer"
        assert vp._ui.fd >= 0
    finally:
        vp.close()
