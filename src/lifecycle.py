"""Pidfile-based single-instance toggle, signal handling, and error reporting."""

import os
import signal
import subprocess
import sys

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

PIDFILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "loupe.pid")


def _write_own_pid():
    with open(PIDFILE, "w") as f:
        f.write(f"{os.getpid()}\n")


def _is_stale(pid):
    """Return True if pid is dead or alive-but-not-loupe (recycled)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True  # ESRCH: dead
    except PermissionError:
        pass  # EPERM: alive, owned by someone else — still counts as alive

    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read()
    except OSError:
        return True  # process vanished between kill(0) and cmdline read

    return b"loupe" not in cmdline


def acquire_pidfile_or_toggle() -> bool:
    if not os.path.exists(PIDFILE):
        _write_own_pid()
        return True

    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        _write_own_pid()
        return True

    if _is_stale(pid):
        _write_own_pid()
        return True

    # Live loupe instance found — toggle it off.
    os.kill(pid, signal.SIGTERM)
    return False


def release_pidfile() -> None:
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return

    if pid != os.getpid():
        return

    try:
        os.remove(PIDFILE)
    except OSError:
        pass


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
