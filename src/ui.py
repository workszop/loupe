"""loupe UI: pure lens-placement math (`compute_layout`) plus the GTK4 widgets
(`LoupeWindow`, `LensWidget`) that render the live magnifier.

This module MUST NOT import portal/framesource — it receives collaborator
objects (a frame source with `.get_texture()`, an `on_quit` callback) via
constructor injection only.
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
# Constants (binding, see .superpowers/sdd/interfaces.md)
# --------------------------------------------------------------------------

LENS_W, LENS_H = 960, 320
MARGIN = 20            # min gap between lens outer edge and source rect
RADIUS = 14             # lens corner radius
BORDER = 2.0            # lens border width (part of lens outer rect!)
ZOOM_MIN, ZOOM_MAX, ZOOM_STEP = 1.5, 8.0, 1.25
ZOOM_DEFAULT = 2.5

_OSD_DURATION_US = 1_200_000  # 1.2s, in GLib monotonic-time microseconds


@dataclass(frozen=True)
class Layout:
    src: tuple[float, float, float, float]   # sx, sy, sw, sh — window coords
    lens: tuple[float, float, float, float]  # lx, ly, LENS_W, LENS_H — window coords


def _clamp(value: float, lo: float, hi: float) -> float:
    if hi < lo:
        return lo
    return max(lo, min(value, hi))


def compute_layout(cx: float, cy: float, zoom: float, win_w: int, win_h: int) -> Layout:
    """Place the lens relative to the cursor so it never overlaps its own
    source rect (the screencast feedback-loop guarantee).

    Algorithm:
    - The source rect is LENS_W/zoom x LENS_H/zoom, centered on the cursor,
      clamped fully inside the window.
    - Preferred lens placement, in order: right of source, left of source,
      below source, above source — whichever has room for MARGIN + the lens
      dimension without leaving the window.
    - If none of the four has that much room (possible when the window is too
      small to seat the lens beside/above/below the source — e.g. the 960-wide
      lens in a window narrower than ~2600px whose height is also too short at
      low zoom), we fall back to whichever direction has the most room.
      Non-overlap is enforced unconditionally as a final safety clamp: even
      when the window cannot contain the lens, the lens's BORDER-expanded
      outer rect is guaranteed never to intersect the source rect — the lens
      is allowed to hang outside the window edge instead, which is harmless
      (no screencast feedback loop). At the deployment resolution
      (1920x1132) the lens always stays fully inside the window; the
      hang-outside path is only reachable in windows smaller than the lens.
    """
    sw = LENS_W / zoom
    sh = LENS_H / zoom
    sx = _clamp(cx - sw / 2, 0.0, win_w - sw)
    sy = _clamp(cy - sh / 2, 0.0, win_h - sh)

    right_room = win_w - (sx + sw)
    left_room = sx
    below_room = win_h - (sy + sh)
    above_room = sy

    need_w = MARGIN + LENS_W
    need_h = MARGIN + LENS_H

    if right_room >= need_w:
        direction = "right"
    elif left_room >= need_w:
        direction = "left"
    elif below_room >= need_h:
        direction = "below"
    elif above_room >= need_h:
        direction = "above"
    else:
        rooms = {
            "right": right_room,
            "left": left_room,
            "below": below_room,
            "above": above_room,
        }
        direction = max(rooms, key=rooms.get)

    if direction in ("right", "left"):
        ly = _clamp(cy - LENS_H / 2, 0.0, win_h - LENS_H)
        if direction == "right":
            lx = _clamp(sx + sw + MARGIN, 0.0, win_w - LENS_W)
            safety_floor = sx + sw + BORDER
            if lx < safety_floor:
                lx = safety_floor
        else:
            lx = _clamp(sx - MARGIN - LENS_W, 0.0, win_w - LENS_W)
            safety_ceiling = sx - LENS_W - BORDER
            if lx > safety_ceiling:
                lx = safety_ceiling
    else:
        lx = _clamp(cx - LENS_W / 2, 0.0, win_w - LENS_W)
        if direction == "below":
            ly = _clamp(sy + sh + MARGIN, 0.0, win_h - LENS_H)
            safety_floor = sy + sh + BORDER
            if ly < safety_floor:
                ly = safety_floor
        else:
            ly = _clamp(sy - MARGIN - LENS_H, 0.0, win_h - LENS_H)
            safety_ceiling = sy - LENS_H - BORDER
            if ly > safety_ceiling:
                ly = safety_ceiling

    return Layout(src=(sx, sy, sw, sh), lens=(lx, ly, float(LENS_W), float(LENS_H)))


# --------------------------------------------------------------------------
# Part B: GTK4 widgets
# --------------------------------------------------------------------------

_css_installed = False


def _install_transparent_css() -> None:
    """Install the display-level CSS provider that makes a maximized,
    undecorated window transparent on COSMIC. Idempotent per process."""
    global _css_installed
    if _css_installed:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string("window, .background { background: transparent; }")
    display = Gdk.Display.get_default()
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _css_installed = True


class LensWidget(Gtk.Widget):
    """Renders the magnifier lens: a zoomed clip of the live frame texture,
    a border, a crosshair at the cursor, and a transient zoom-level OSD."""

    def __init__(self, window: "LoupeWindow"):
        super().__init__()
        self._window = window
        self.set_hexpand(True)
        self.set_vexpand(True)

    def do_snapshot(self, snapshot: Gsk.Snapshot) -> None:  # noqa: N802 (GTK vfunc name)
        win = self._window

        if win.calibrating:
            magenta = Gdk.RGBA()
            magenta.parse("#FF00FF")
            snapshot.append_color(magenta, Graphene.Rect().init(0, 0, 16, 16))
            return

        if win.cursor_pos is None:
            return

        tex = win.frame_source.get_texture()
        if tex is None:
            return

        cx, cy = win.cursor_pos
        win_w = self.get_width()
        win_h = self.get_height()
        if win_w <= 0 or win_h <= 0:
            return

        layout = compute_layout(cx, cy, win.zoom, win_w, win_h)
        sx, sy, sw, sh = layout.src
        lx, ly, lw, lh = layout.lens

        tex_w = tex.get_width()
        tex_h = tex.get_height()
        zoom = win.zoom
        ox, oy = win.frame_offset

        dest = Graphene.Rect().init(
            lx - (sx + ox) * zoom,
            ly - (sy + oy) * zoom,
            tex_w * zoom,
            tex_h * zoom,
        )

        lens_rounded = Gsk.RoundedRect()
        lens_rounded.init_from_rect(Graphene.Rect().init(lx, ly, lw, lh), RADIUS)

        snapshot.push_rounded_clip(lens_rounded)
        filt = Gsk.ScalingFilter.LINEAR if zoom < 3 else Gsk.ScalingFilter.NEAREST
        snapshot.append_scaled_texture(tex, filt, dest)
        snapshot.pop()

        border_widths = [BORDER, BORDER, BORDER, BORDER]
        border_color = Gdk.RGBA()
        border_color.parse("rgba(255,255,255,0.85)")
        snapshot.append_border(
            lens_rounded, border_widths, [border_color] * 4
        )

        # Crosshair, at the cursor's position within the (scaled) lens.
        crosshair_x = lx + (cx - sx) * zoom
        crosshair_y = ly + (cy - sy) * zoom
        arm = 12
        white = Gdk.RGBA()
        white.parse("rgba(255,255,255,0.5)")
        shadow = Gdk.RGBA()
        shadow.parse("rgba(0,0,0,0.5)")

        # 1px shadow offset, then the crosshair itself.
        snapshot.append_color(
            shadow, Graphene.Rect().init(crosshair_x - arm / 2 + 1, crosshair_y - 0.5 + 1, arm, 1)
        )
        snapshot.append_color(
            shadow, Graphene.Rect().init(crosshair_x - 0.5 + 1, crosshair_y - arm / 2 + 1, 1, arm)
        )
        snapshot.append_color(
            white, Graphene.Rect().init(crosshair_x - arm / 2, crosshair_y - 0.5, arm, 1)
        )
        snapshot.append_color(
            white, Graphene.Rect().init(crosshair_x - 0.5, crosshair_y - arm / 2, 1, arm)
        )

        if GLib.get_monotonic_time() < win.osd_until:
            self._draw_osd(snapshot, lx, ly, win.zoom)

    def _draw_osd(self, snapshot: Gsk.Snapshot, lx: float, ly: float, zoom: float) -> None:
        pad = 8
        pill_h = 28
        px, py = lx + pad, ly + pad

        layout = self.create_pango_layout(f"{zoom:.2f}x")
        layout.set_alignment(Pango.Alignment.CENTER)
        text_w, _text_h = layout.get_pixel_size()
        pill_w = max(64, text_w + 20)
        white = Gdk.RGBA()
        white.parse("white")

        pill_rounded = Gsk.RoundedRect()
        pill_rounded.init_from_rect(Graphene.Rect().init(px, py, pill_w, pill_h), 8)
        dark = Gdk.RGBA()
        dark.parse("rgba(0,0,0,0.7)")
        snapshot.push_rounded_clip(pill_rounded)
        snapshot.append_color(dark, Graphene.Rect().init(px, py, pill_w, pill_h))
        snapshot.pop()

        snapshot.save()
        snapshot.translate(Graphene.Point().init(px + 10, py + 5))
        snapshot.append_layout(layout, white)
        snapshot.restore()


class LoupeWindow(Gtk.ApplicationWindow):
    def __init__(self, *, application, frame_source, on_quit):
        super().__init__(application=application)
        self.frame_source = frame_source
        self.on_quit = on_quit

        self.cursor_pos: tuple[float, float] | None = None
        self.zoom = ZOOM_DEFAULT
        self.osd_until = 0
        self.frame_offset = (0.0, 0.0)
        self.calibrating = True

        self.set_decorated(False)
        self.maximize()

        _install_transparent_css()

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
        # Ctrl+equal / Ctrl+plus / plus / equal / KP_Add -> zoom_in
        # Ctrl+minus / minus / KP_Subtract -> zoom_out
        # (the Ctrl modifier doesn't change the keyval name GDK reports, so
        # it's irrelevant here — "equal" already covers both = and Ctrl+=.)
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

    # -- calibration / frame updates -------------------------------------

    def set_frame_offset(self, ox: float, oy: float) -> None:
        self.frame_offset = (ox, oy)
        self.calibrating = False
        self.lens.queue_draw()

    def notify_frame(self) -> None:
        self.lens.queue_draw()


# --------------------------------------------------------------------------
# Part C: manual harness
# --------------------------------------------------------------------------


class _FakeFrameSource:
    """Builds one procedural 1920x1200 BGRx texture: an 8x8px checkerboard
    with a red diagonal stripe and a fine dotted pattern, for manual
    smoke-testing the widgets without portal/framesource."""

    WIDTH, HEIGHT = 1920, 1200

    def __init__(self):
        self._texture = self._build_texture()

    def _build_texture(self):
        w, h = self.WIDTH, self.HEIGHT
        stride = w * 4
        buf = bytearray(stride * h)
        for y in range(h):
            row = y * stride
            for x in range(w):
                i = row + x * 4
                checker = ((x // 8) + (y // 8)) % 2
                base = 200 if checker else 60
                b, g, r = base, base, base
                if abs((x - y) % 200) < 3:
                    r, g, b = 220, 30, 30
                if x % 37 == 0 and y % 37 == 0:
                    r = g = b = 255
                buf[i] = b
                buf[i + 1] = g
                buf[i + 2] = r
                buf[i + 3] = 255
        gbytes = GLib.Bytes.new(bytes(buf))
        return Gdk.MemoryTexture.new(
            w, h, Gdk.MemoryFormat.B8G8R8X8, gbytes, stride
        )

    def get_texture(self):
        return self._texture


def main():
    print("loupe manual harness")
    print("  Esc / click  -> quit")
    print("  scroll / +/- -> zoom")
    print("  first 1s     -> calibration mode (magenta square only)")

    app = Gtk.Application(application_id="dev.andrzey.loupe.manualharness")

    def on_quit():
        app.quit()

    def on_activate(app):
        frame_source = _FakeFrameSource()
        window = LoupeWindow(application=app, frame_source=frame_source, on_quit=on_quit)
        window.present()
        GLib.timeout_add(1000, lambda: (window.set_frame_offset(0, 68), False)[1])

    app.connect("activate", on_activate)
    app.run(None)


if __name__ == "__main__":
    main()
