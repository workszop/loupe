"""Signal handling and error reporting.

Single-instancing and toggling are NOT handled here: the loupe runs as a
transient systemd user unit (see tools/loupe-toggle), so a second toggle is
`systemctl --user stop loupe`, which lands in install_signal_handlers as
SIGTERM.
"""

import signal
import subprocess
import sys

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib


def install_signal_handlers(cleanup_cb) -> None:
    def handler(*_args):
        cleanup_cb()
        return GLib.SOURCE_REMOVE

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, handler)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, handler)


def fail(message: str, hint: str = "") -> None:
    text = f"loupe: {message}"
    if hint:
        text += f"\n{hint}"
    print(text, file=sys.stderr)

    try:
        body = message + (f"\n{hint}" if hint else "")
        subprocess.run(
            ["notify-send", "-a", "loupe", "loupe", body], timeout=2
        )
    except Exception:
        pass
