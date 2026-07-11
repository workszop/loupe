#!/usr/bin/env python3
"""loupe entry point: a freeze-frame screen magnifier.

Flow: acquire the single-instance pidfile (or toggle off a running instance),
grab a silent full-screen screenshot, open a fullscreen Gtk.Application window
showing that frozen image, and draw a magnifier lens centered on the cursor.
Esc / click / Ctrl+Q / a second Super+Z all dismiss it.

Usage: loupe.py
On COSMIC, bind Super+Z to run this script; a second Super+Z toggles it off.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import signal
import subprocess
import sys
import tempfile

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import GLib, Gdk, Gio, Graphene, Gsk, Gtk, Pango

# ==== src/lifecycle.py ====
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


# ==== src/capture.py ====
def grab_screenshot() -> Gdk.Texture:
    """Capture the whole screen and return it as a Gdk.Texture.

    Raises RuntimeError if the screenshot tool fails or produces no file.
    """
    tmpdir = tempfile.mkdtemp(prefix="loupe-shot-")
    try:
        result = subprocess.run(
            [
                "cosmic-screenshot",
                "--interactive=false",
                "--notify=false",
                "-s",
                tmpdir,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        path = result.stdout.strip()
        if not path or not os.path.exists(path):
            # Some builds may not echo the path; fall back to newest file.
            candidates = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)]
            candidates = [p for p in candidates if os.path.isfile(p)]
            if not candidates:
                raise RuntimeError(
                    "screenshot failed: " + (result.stderr.strip() or "no output file")
                )
            path = max(candidates, key=os.path.getmtime)

        texture = Gdk.Texture.new_from_filename(path)
        return texture
    finally:
        # Best-effort cleanup of the temp PNG + dir; the texture holds its own
        # decoded copy so the file is no longer needed.
        try:
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)
        except OSError:
            pass


# ==== src/ui.py ====
# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

LENS_W, LENS_H = 1440, 480
RADIUS = 22             # lens corner radius
BORDER = 2.0            # lens border width
ZOOM_MIN, ZOOM_MAX, ZOOM_STEP = 1.5, 8.0, 1.25
ZOOM_DEFAULT = 2.5

_OSD_DURATION_US = 1_200_000  # 1.2s, in GLib monotonic-time microseconds


@dataclass(frozen=True)
class Layout:
    src: tuple[float, float, float, float]   # sx, sy, sw, sh — the magnified region
    lens: tuple[float, float, float, float]  # lx, ly, LENS_W, LENS_H — centered on cursor


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(value, hi))


def compute_layout(cx: float, cy: float, zoom: float) -> Layout:
    """Place the lens centered on the cursor.

    The source rect (the area being magnified) is LENS_W/zoom x LENS_H/zoom,
    centered on the cursor. The lens itself is LENS_W x LENS_H, also centered
    on the cursor. Nothing is clamped to the screen: the lens follows the
    cursor exactly and may extend past the screen edge (the off-screen part is
    simply not visible), so movement stays smooth and never jumps.
    """
    sw = LENS_W / zoom
    sh = LENS_H / zoom
    return Layout(
        src=(cx - sw / 2, cy - sh / 2, sw, sh),
        lens=(cx - LENS_W / 2, cy - LENS_H / 2, float(LENS_W), float(LENS_H)),
    )


# --------------------------------------------------------------------------
# GTK4 widgets
# --------------------------------------------------------------------------


class LensWidget(Gtk.Widget):
    """Draws the frozen screenshot as a full-window background, then a magnifier
    lens (a zoomed clip of the same screenshot) centered on the cursor, with a
    thin border and a transient zoom-level OSD."""

    def __init__(self, window: "LoupeWindow"):
        super().__init__()
        self._window = window
        self.set_hexpand(True)
        self.set_vexpand(True)

    def do_snapshot(self, snapshot: Gsk.Snapshot) -> None:  # noqa: N802 (GTK vfunc name)
        win = self._window
        tex = win.texture
        win_w = self.get_width()
        win_h = self.get_height()
        if win_w <= 0 or win_h <= 0 or tex is None:
            return

        # Background: the frozen screenshot, stretched to fill the window 1:1.
        snapshot.append_scaled_texture(
            tex, Gsk.ScalingFilter.LINEAR, Graphene.Rect().init(0, 0, win_w, win_h)
        )

        if win.cursor_pos is None:
            return

        cx, cy = win.cursor_pos
        zoom = win.zoom
        _, _, lw, lh = (0, 0, LENS_W, LENS_H)
        lx = cx - LENS_W / 2
        ly = cy - LENS_H / 2

        # Magnified layer: the whole (window-space) image scaled by `zoom`,
        # positioned so the pixel under the cursor stays under the cursor.
        dest = Graphene.Rect().init(
            cx * (1.0 - zoom),
            cy * (1.0 - zoom),
            win_w * zoom,
            win_h * zoom,
        )

        lens_rounded = Gsk.RoundedRect()
        lens_rounded.init_from_rect(Graphene.Rect().init(lx, ly, lw, lh), RADIUS)

        snapshot.push_rounded_clip(lens_rounded)
        filt = Gsk.ScalingFilter.LINEAR if zoom < 3 else Gsk.ScalingFilter.NEAREST
        snapshot.append_scaled_texture(tex, filt, dest)
        snapshot.pop()

        border_color = Gdk.RGBA()
        border_color.parse("rgba(255,255,255,0.85)")
        snapshot.append_border(
            lens_rounded, [BORDER, BORDER, BORDER, BORDER], [border_color] * 4
        )

        if GLib.get_monotonic_time() < win.osd_until:
            self._draw_osd(snapshot, win_w, zoom)

    def _draw_osd(self, snapshot: Gsk.Snapshot, win_w: int, zoom: float) -> None:
        """Zoom-level pill, centered near the top of the screen so it stays
        visible regardless of where the (possibly off-screen) lens is."""
        pill_h = 30
        layout = self.create_pango_layout(f"{zoom:.2f}x")
        layout.set_alignment(Pango.Alignment.CENTER)
        text_w, _text_h = layout.get_pixel_size()
        pill_w = max(72, text_w + 24)
        px = win_w / 2 - pill_w / 2
        py = 40.0

        pill_rounded = Gsk.RoundedRect()
        pill_rounded.init_from_rect(Graphene.Rect().init(px, py, pill_w, pill_h), 10)
        dark = Gdk.RGBA()
        dark.parse("rgba(0,0,0,0.72)")
        snapshot.push_rounded_clip(pill_rounded)
        snapshot.append_color(dark, Graphene.Rect().init(px, py, pill_w, pill_h))
        snapshot.pop()

        white = Gdk.RGBA()
        white.parse("white")
        snapshot.save()
        snapshot.translate(
            Graphene.Point().init(px + (pill_w - text_w) / 2, py + 5)
        )
        snapshot.append_layout(layout, white)
        snapshot.restore()


class LoupeWindow(Gtk.ApplicationWindow):
    def __init__(self, *, application, texture, on_quit):
        super().__init__(application=application)
        self.texture = texture
        self.on_quit = on_quit

        self.cursor_pos: tuple[float, float] | None = None
        self.zoom = ZOOM_DEFAULT
        self.osd_until = 0

        self.set_decorated(False)
        self.fullscreen()

        self.set_cursor(Gdk.Cursor.new_from_name("crosshair"))

        self.lens = LensWidget(self)
        self.set_child(self.lens)

        self.connect("close-request", self._on_close_request)

        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

        key = Gtk.EventControllerKey.new()
        key.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key)

        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        click = Gtk.GestureClick.new()
        click.connect("released", self._on_click_released)
        self.add_controller(click)

    # -- controllers --------------------------------------------------

    def _on_motion(self, _controller, x, y):
        self.cursor_pos = (x, y)
        self.lens.queue_draw()

    def _on_leave(self, _controller):
        self.cursor_pos = None
        self.lens.queue_draw()

    def _on_key_pressed(self, _controller, keyval, _keycode, _state):
        name = Gdk.keyval_name(keyval)
        ctrl = bool(_state & Gdk.ModifierType.CONTROL_MASK)

        if name == "Escape" or (ctrl and name in ("q", "Q")):
            self.on_quit()
            return True
        if name in ("equal", "plus", "KP_Add"):
            self.zoom_in()
            return True
        if name in ("minus", "KP_Subtract"):
            self.zoom_out()
            return True
        return False

    def _on_scroll(self, _controller, _dx, dy):
        if dy < 0:
            self.zoom_in()
        elif dy > 0:
            self.zoom_out()
        return True

    def _on_click_released(self, _gesture, _n_press, _x, _y):
        self.on_quit()

    def _on_close_request(self, _window):
        self.on_quit()
        return True

    # -- zoom -----------------------------------------------------------

    def zoom_in(self):
        self.zoom = _clamp(self.zoom * ZOOM_STEP, ZOOM_MIN, ZOOM_MAX)
        self._bump_osd()

    def zoom_out(self):
        self.zoom = _clamp(self.zoom / ZOOM_STEP, ZOOM_MIN, ZOOM_MAX)
        self._bump_osd()

    def _bump_osd(self):
        self.osd_until = GLib.get_monotonic_time() + _OSD_DURATION_US
        self.lens.queue_draw()
        GLib.timeout_add(1250, self._osd_expired)

    def _osd_expired(self):
        self.lens.queue_draw()
        return False


# --------------------------------------------------------------------------
# Manual harness: `python3 src/ui.py` shows the window over a procedural
# texture (no screenshot needed) so the lens/zoom/quit can be checked by hand.
# --------------------------------------------------------------------------


def _build_demo_texture(width=1920, height=1200):
    stride = width * 4
    buf = bytearray(stride * height)
    for y in range(height):
        row = y * stride
        for x in range(width):
            i = row + x * 4
            checker = ((x // 16) + (y // 16)) % 2
            base = 210 if checker else 70
            b = g = r = base
            if abs((x - y) % 240) < 3:
                r, g, b = 220, 40, 40
            buf[i] = b
            buf[i + 1] = g
            buf[i + 2] = r
            buf[i + 3] = 255
    return Gdk.MemoryTexture.new(
        width, height, Gdk.MemoryFormat.B8G8R8X8, GLib.Bytes.new(bytes(buf)), stride
    )


def _demo_main():
    from gi.repository import Gio

    app = Gtk.Application(
        application_id="dev.andrzey.loupe.demo", flags=Gio.ApplicationFlags.NON_UNIQUE
    )

    def on_activate(a):
        tex = _build_demo_texture()
        win = LoupeWindow(application=a, texture=tex, on_quit=a.quit)
        win.present()
        print("loupe UI demo: move mouse to magnify, +/- to zoom, Esc to quit.")

    app.connect("activate", on_activate)
    return app.run(None)


# ==== src/main.py ====
# NOTE: `from X import name` (not `import X`) deliberately — after
# tools/build.py bundles the src modules into one flat loupe.py, there is no
# separate `capture`/`ui`/`lifecycle` namespace to qualify against.

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
