"""loupe entry point: wires portal/framesource/ui/lifecycle together.

Flow: acquire the single-instance pidfile (or toggle off a running instance),
check pipewiresrc is available, open a Gtk.Application, drive the portal
handshake on activate, and on the portal's first stream build a FrameSource +
LoupeWindow. A short calibration pass (`locate_marker`) maps window
coordinates to frame coordinates by locating a magenta marker the window
itself draws in its first frames.

Usage: loupe.py [--test-portal] [--smoke]
  --test-portal  run only the portal handshake (src/portal.py entry point)
                 and print node_id/fd/props; pops the portal consent dialog.
  --smoke        internal: skip the portal, run a synthetic videotestsrc
                 pipeline instead, and skip the pidfile toggle. Used by the
                 single-file build's smoke test; not for interactive use.

On COSMIC, bind Super+Z to run this script; a second Super+Z toggles it off.
"""
from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")

from gi.repository import Gio, GLib, Gtk  # noqa: E402

# NOTE: these are `from X import name` (not `import X`) deliberately — after
# tools/build.py bundles all five src modules into one flat loupe.py file,
# there is no separate `portal`/`framesource`/`ui`/`lifecycle` namespace to
# qualify against; only the bare names it imports here still resolve.
from framesource import FrameSource, have_pipewiresrc  # noqa: E402
from lifecycle import (  # noqa: E402
    acquire_pidfile_or_toggle,
    fail,
    install_signal_handlers,
    release_pidfile,
)
from portal import PortalScreenCast, _run_test_portal  # noqa: E402
from ui import LoupeWindow  # noqa: E402

APP_ID = "dev.andrzey.loupe"

MARKER_SIZE = 16
SEARCH_ROWS = 300
SEARCH_COLS = 400
CALIBRATION_MAX_FRAMES = 30
CALIBRATION_TIMEOUT_S = 2.5

_SMOKE_PIPELINE = (
    "videotestsrc is-live=true ! videoconvert ! "
    "video/x-raw,format=BGRx,width=1280,height=720 ! "
    "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
)


# --------------------------------------------------------------------------
# Calibration: pure function, TDD'd against synthetic byte buffers.
# --------------------------------------------------------------------------


def _row_is_marker(data: bytes, stride: int, x: int, y: int, size: int) -> bool:
    row_start = y * stride + x * 4
    for i in range(size):
        off = row_start + i * 4
        if off + 2 >= len(data):
            return False
        if data[off] != 0xFF or data[off + 1] != 0x00 or data[off + 2] != 0xFF:
            return False
    return True


def locate_marker(data: bytes, w: int, h: int, stride: int) -> tuple[int, int] | None:
    """Find the top-left corner of the 16x16 solid magenta (#FF00FF) block
    LoupeWindow draws at window (0,0) while calibrating.

    Searches rows 0..min(h,300), cols 0..min(w,400). A match requires a
    16-pixel horizontal run of magenta AND matching spot-check runs on rows
    +4/+8/+15 (guards against coincidental magenta elsewhere in the frame).
    Returns (x, y) in frame pixel coordinates, or None if not found.
    """
    x_limit = min(w, SEARCH_COLS)
    y_limit = min(h, SEARCH_ROWS)

    for y in range(y_limit):
        if y + (MARKER_SIZE - 1) >= h:
            continue
        for x in range(x_limit):
            if x + MARKER_SIZE > w:
                continue
            if not _row_is_marker(data, stride, x, y, MARKER_SIZE):
                continue
            if not _row_is_marker(data, stride, x, y + 4, MARKER_SIZE):
                continue
            if not _row_is_marker(data, stride, x, y + 8, MARKER_SIZE):
                continue
            if not _row_is_marker(data, stride, x, y + 15, MARKER_SIZE):
                continue
            return (x, y)
    return None


# --------------------------------------------------------------------------
# Application wiring
# --------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    if "--test-portal" in argv:
        try:
            _run_test_portal()
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1
        return 0

    smoke = "--smoke" in argv

    if not smoke:
        if not acquire_pidfile_or_toggle():
            return 0

        if not have_pipewiresrc():
            fail(
                "GStreamer PipeWire plugin missing",
                hint="sudo apt install gstreamer1.0-pipewire gir1.2-gst-plugins-base-1.0",
            )
            release_pidfile()
            return 1

    app = Gtk.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
    state = {"fs": None, "portal": None, "cleaned_up": False, "exit_code": None}

    def cleanup():
        if state["cleaned_up"]:
            return
        state["cleaned_up"] = True
        if state["fs"] is not None:
            state["fs"].stop()
        if state["portal"] is not None:
            state["portal"].close()
        if not smoke:
            release_pidfile()
        app.quit()

    def make_calibration(window):
        counter = {"frames": 0, "done": False, "timeout_id": None}

        def finish_fallback():
            if counter["done"]:
                return GLib.SOURCE_REMOVE
            counter["done"] = True
            fs = state["fs"]
            frame = fs.get_latest_frame() if fs is not None else None
            frame_h = frame[2] if frame is not None else window.get_height()
            print(
                "loupe: calibration marker not found, falling back to "
                "ox=0, oy=frame_h - window_height",
                file=sys.stderr,
            )
            window.set_frame_offset(0, frame_h - window.get_height())
            return GLib.SOURCE_REMOVE

        counter["timeout_id"] = GLib.timeout_add(
            int(CALIBRATION_TIMEOUT_S * 1000), finish_fallback
        )

        def on_frame():
            if not counter["done"]:
                fs = state["fs"]
                frame = fs.get_latest_frame() if fs is not None else None
                if frame is not None:
                    data, w, h, stride, _fmt = frame
                    found = locate_marker(data, w, h, stride)
                    if found is not None:
                        counter["done"] = True
                        if counter["timeout_id"] is not None:
                            GLib.source_remove(counter["timeout_id"])
                            counter["timeout_id"] = None
                        mx, my = found
                        window.set_frame_offset(mx, my)
                    else:
                        counter["frames"] += 1
                        if counter["frames"] >= CALIBRATION_MAX_FRAMES:
                            if counter["timeout_id"] is not None:
                                GLib.source_remove(counter["timeout_id"])
                                counter["timeout_id"] = None
                            finish_fallback()
            window.notify_frame()

        return on_frame

    def on_activate(app):
        install_signal_handlers(cleanup)

        def start_frame_source(node_id, fd, props):
            def on_fs_error(message):
                state["exit_code"] = 1
                fail(message)
                cleanup()

            # window doesn't exist until after FrameSource is constructed, and
            # calibration needs the window — dispatch through a holder that's
            # populated once the window is built.
            frame_cb_holder = {"fn": lambda: None}

            def on_frame():
                frame_cb_holder["fn"]()

            pipeline_override = _SMOKE_PIPELINE if smoke else None
            fs = FrameSource(
                fd, node_id, on_frame, on_fs_error, pipeline_override=pipeline_override
            )
            state["fs"] = fs
            fs.start()

            window = LoupeWindow(application=app, frame_source=fs, on_quit=cleanup)
            frame_cb_holder["fn"] = make_calibration(window)
            window.present()
            app.release()

        on_ready = start_frame_source

        def on_portal_error(code, message):
            if code == 1:
                cleanup()
                return
            state["exit_code"] = 1
            fail(message)
            cleanup()

        if smoke:
            app.hold()
            start_frame_source(0, -1, {})
            return

        ps = PortalScreenCast()
        state["portal"] = ps
        app.hold()
        ps.start(on_ready, on_portal_error)

    app.connect("activate", on_activate)
    run_result = app.run(None)
    return state["exit_code"] if state["exit_code"] is not None else run_result


if __name__ == "__main__":
    sys.exit(main(sys.argv))
