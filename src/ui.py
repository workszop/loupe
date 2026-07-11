"""loupe UI: pure lens-placement math (`compute_layout`) plus the GTK4 widgets
(`LoupeWindow`, `LensWidget`) that render the freeze-frame magnifier.

Design: the window is maximized and opaque, showing a frozen screenshot of the
desktop at 1:1 (maximized, not fullscreen — COSMIC animates fullscreen
enter/exit with a black transition). A magnifier lens is drawn centered ON the
cursor (like a real magnifying glass), showing the screenshot magnified.
Because the background is a static snapshot, the lens can sit directly over the
point it magnifies with no screencast feedback loop. The lens tracks the cursor
smoothly and is allowed to slide off the screen edge rather than jumping to stay
in view.

This module receives its screenshot texture and an `on_quit` callback via the
LoupeWindow constructor; it imports nothing from the rest of the app.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Pango", "1.0")

from dataclasses import dataclass

from gi.repository import Gdk, GLib, Gsk, Gtk, Graphene, Pango

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

LENS_W, LENS_H = 1440, 480
RADIUS = 22             # lens corner radius
BORDER = 2.0            # lens border width
ZOOM_MIN, ZOOM_MAX, ZOOM_STEP = 1.5, 8.0, 1.25
ZOOM_DEFAULT = 2.5

_OSD_DURATION_US = 1_200_000  # 1.2s, in GLib monotonic-time microseconds


def _rgba(spec: str) -> Gdk.RGBA:
    color = Gdk.RGBA()
    color.parse(spec)
    return color


# Parsed once; do_snapshot runs on every mouse motion, so no per-frame parsing.
_BORDER_COLOR = _rgba("rgba(255,255,255,0.85)")
_OSD_BG_COLOR = _rgba("rgba(0,0,0,0.72)")
_OSD_TEXT_COLOR = _rgba("white")


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

        tex_w = tex.get_width()
        tex_h = tex.get_height()
        # The window is maximized (covers the work area, below the top panel);
        # the screenshot covers the whole screen. Shift the screenshot up by the
        # panel height so window pixel (x, y) shows screen pixel (x, y+offset_y).
        # (Maximized rather than fullscreen: COSMIC animates fullscreen
        # enter/exit with a black transition; maximize does not.)
        offset_y = max(0, tex_h - win_h)

        # Background: the frozen screenshot at native size, shifted up so the
        # work-area portion fills the window 1:1.
        snapshot.append_texture(tex, Graphene.Rect().init(0, -offset_y, tex_w, tex_h))

        if win.cursor_pos is None:
            return

        cx, cy = win.cursor_pos
        zoom = win.zoom
        layout = compute_layout(cx, cy, zoom)
        sx, sy, _sw, _sh = layout.src
        lx, ly, lw, lh = layout.lens

        # Magnified layer: the whole texture scaled by `zoom`, positioned so
        # the src rect lands exactly on the lens rect — the pixel under the
        # cursor stays under the cursor. (sy + offset_y converts the src rect
        # from window coords to texture coords, as for the background.)
        dest = Graphene.Rect().init(
            lx - sx * zoom,
            ly - (sy + offset_y) * zoom,
            tex_w * zoom,
            tex_h * zoom,
        )

        lens_rounded = Gsk.RoundedRect()
        lens_rounded.init_from_rect(Graphene.Rect().init(lx, ly, lw, lh), RADIUS)

        snapshot.push_rounded_clip(lens_rounded)
        filt = Gsk.ScalingFilter.LINEAR if zoom < 3 else Gsk.ScalingFilter.NEAREST
        snapshot.append_scaled_texture(tex, filt, dest)
        snapshot.pop()

        snapshot.append_border(
            lens_rounded, [BORDER, BORDER, BORDER, BORDER], [_BORDER_COLOR] * 4
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
        snapshot.push_rounded_clip(pill_rounded)
        snapshot.append_color(_OSD_BG_COLOR, Graphene.Rect().init(px, py, pill_w, pill_h))
        snapshot.pop()

        snapshot.save()
        snapshot.translate(
            Graphene.Point().init(px + (pill_w - text_w) / 2, py + 5)
        )
        snapshot.append_layout(layout, _OSD_TEXT_COLOR)
        snapshot.restore()


class LoupeWindow(Gtk.ApplicationWindow):
    def __init__(self, *, application, texture, on_quit, on_click_through=None):
        super().__init__(application=application)
        self.texture = texture
        self.on_quit = on_quit
        # on_click_through(): dismiss the loupe and fire a real click where the
        # cursor is aimed. If None, a left click just dismisses (no synthesis).
        self.on_click_through = on_click_through

        self.cursor_pos: tuple[float, float] | None = None
        self.zoom = ZOOM_DEFAULT
        self.osd_until = 0
        self._osd_timer = 0

        self.set_decorated(False)
        self.maximize()

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
        click.set_button(0)  # 0 = listen for any button, branch in the handler
        click.connect("released", self._on_click_released)
        self.add_controller(click)

    # -- controllers --------------------------------------------------

    def _on_motion(self, _controller, x, y):
        self.cursor_pos = (x, y)
        self.lens.queue_draw()

    def _on_leave(self, _controller):
        self.cursor_pos = None
        self.lens.queue_draw()

    def _on_key_pressed(self, _controller, keyval, _keycode, state):
        name = Gdk.keyval_name(keyval)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)

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

    def _on_click_released(self, gesture, _n_press, _x, _y):
        button = gesture.get_current_button()
        # Left click (1): act on the target under the cursor, then dismiss.
        # Any other button (e.g. right): just dismiss (cancel).
        if button == 1 and self.on_click_through is not None:
            self.on_click_through()
        else:
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
        # One live expiry timer: rescheduling on every zoom step (instead of
        # stacking a timeout per step) keeps rapid scroll-zoom cheap.
        if self._osd_timer:
            GLib.source_remove(self._osd_timer)
        self._osd_timer = GLib.timeout_add(1250, self._osd_expired)

    def _osd_expired(self):
        self._osd_timer = 0
        self.lens.queue_draw()
        return GLib.SOURCE_REMOVE


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


if __name__ == "__main__":
    import sys

    sys.exit(_demo_main())
