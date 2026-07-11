# Task 3: `src/ui.py` — compute_layout + LoupeWindow + LensWidget

Read `.superpowers/sdd/interfaces.md` first — constants, `Layout`,
`compute_layout` signature and the `LoupeWindow` contract are binding.

## Part A: `compute_layout` (pure math — strict TDD, write tests first)

Requirements:
- Source rect: `sw, sh = LENS_W/zoom, LENS_H/zoom`, centered on cursor,
  clamped into `[0, win_w] x [0, win_h]`.
- Lens placement, in order of preference:
  1. right of source: `lx = sx + sw + MARGIN`
  2. left of source: `lx = sx - MARGIN - LENS_W` (when right side overflows)
  3. below source / above source with x centered on cursor and clamped
     (only when neither horizontal position fits — defensive; can't happen at
     1920 width with these constants, but must be correct anyway)
- Vertical position for horizontal placements: `ly = clamp(cy - LENS_H/2, 0, win_h - LENS_H)`.
- **HARD GUARANTEE (the whole point):** lens outer rect = lens expanded by
  BORDER on all sides must NOT intersect the source rect. Also lens must be
  fully inside the window when the window is large enough (>= 1280x720).

Tests (`tests/test_layout.py`):
- property sweep: for zoom in {1.5, 2.0, 2.5, 4.0, 8.0} × cursor positions on a
  17x11 grid over a 1920x1132 window (include exact corners and edges):
  assert non-intersection of lens-outer vs src, lens fully in window,
  src fully in window, src contains... note: when cursor is clamped near an
  edge the SOURCE rect no longer centers the cursor but must still CONTAIN the
  cursor point. Assert that too.
- also sweep window size 1280x720 and 2560x1440.
- exact-value spot checks for a couple of hand-computed cases (e.g. cursor at
  (960, 566), zoom 2.5).

## Part B: widgets (manual-run harness, no pytest for GTK parts)

`LoupeWindow(Gtk.ApplicationWindow)`:
- ctor kwargs: `application`, `frame_source` (only calls `.get_texture()`),
  `on_quit` callable. Sets decorated False, maximizes. Does NOT call fullscreen().
- Installs the display-level CSS provider (transparent background — exact
  recipe in interfaces.md Global constraints) — install once per process
  (module-level guard), not per window.
- Crosshair cursor: `self.set_cursor(Gdk.Cursor.new_from_name("crosshair"))`.
- Child is `LensWidget(Gtk.Widget)` (vexpand/hexpand); the window wires its own
  controllers (they can live on the window):
  - `Gtk.EventControllerMotion` "motion" → store cursor pos, queue_draw.
    Also "leave" → hide lens (cursor pos = None), queue_draw.
  - `Gtk.EventControllerKey` "key-pressed": Esc → on_quit(); Ctrl+equal,
    Ctrl+plus, plus, equal, KP_Add → zoom_in; Ctrl+minus, minus, KP_Subtract →
    zoom_out; return True when handled.
  - `Gtk.EventControllerScroll(VERTICAL)`: dy < 0 → zoom_in, dy > 0 → zoom_out.
  - `Gtk.GestureClick` "released" → on_quit().
- Zoom state on the window: `zoom = ZOOM_DEFAULT`, `zoom_in/zoom_out` multiply/
  divide by ZOOM_STEP, clamp [ZOOM_MIN, ZOOM_MAX], set `osd_until =
  GLib.get_monotonic_time() + 1_200_000`, queue_draw, and schedule one redraw
  shortly after expiry (GLib.timeout_add(1250, ...) returning False).
- `set_frame_offset(ox, oy)` / calibration mode per interfaces.md.
- `notify_frame()` → `queue_draw()`.

`LensWidget.do_snapshot(snapshot)` — GPU path, no Cairo:
- calibration mode: only the 16x16 #FF00FF `append_color` square at (0,0); return.
- no cursor yet → nothing.
- `tex = frame_source.get_texture()`; None → nothing.
- `layout = compute_layout(cx, cy, zoom, get_width(), get_height())`.
- frame scale factors `fx = tex.get_width()/win_w`... NO — the frame is the
  whole 1920x1200 output while the window is 1920x1132 at offset (ox,oy):
  frame pixels map 1:1 to window pixels at 100% scale, so use fx = 1.0 for now
  BUT write the transform generally: the texture must be drawn so that frame
  coord `(wx + ox, wy + oy)` lands on window coord `wx, wy` scaled by zoom
  around the source rect. Concretely:
  `dest = Graphene.Rect().init(lx - (sx + ox) * zoom, ly - (sy + oy) * zoom,
                               tex_w * zoom, tex_h * zoom)`
  then `push_rounded_clip(lens rounded rect)`,
  `append_scaled_texture(tex, LINEAR if zoom < 3 else NEAREST, dest)`, `pop()`.
- Border: `append_border` on the lens rounded rect, BORDER width, color
  rgba(255,255,255,0.85) with a 1px inner darker line optional — keep simple.
- Crosshair: two thin `append_color` rects centered at
  `(lx + (cx - sx) * zoom, ly + (cy - sy) * zoom)`, ~12px arms, 50% alpha white
  plus 1px black offset shadow for visibility (or one gray) — keep simple.
- OSD while `GLib.get_monotonic_time() < osd_until`: pill at lens top-left
  inside: rounded rect `append_color` dark 70% alpha + zoom text like `2.5x`
  via `self.create_pango_layout`; position layout with
  `snapshot.translate(Graphene.Point().init(px, py))` + `snapshot.append_layout(layout, white)`
  (in a save/restore), GTK 4.14 has append_layout.

## Part C: manual harness (`python3 src/ui.py`)

Running the module directly opens the window with a FakeFrameSource:
build one 1920x1200 `Gdk.MemoryTexture` procedurally (e.g. 8x8px checkerboard
with a red diagonal stripe and a text-like fine pattern; bytes generated in a
loop, BGRx) and `set_frame_offset(0, 68)` after 1s to exit calibration mode.
This lets a human verify lens/zoom/OSD/quit interactively. Print instructions
to stdout. You can run it headless-ish yourself for smoke (it should map
without traceback for 3s under `timeout 3`); full visual check happens at
integration.

## Environment notes

GTK 4.14 (Pop!_OS 24.04). `Gsk.ScalingFilter`, `append_scaled_texture`,
`append_layout`, `push_rounded_clip` all available. Wayland/COSMIC: maximized
undecorated windows are transparent with the CSS recipe (verified by spike);
window will be 1920x1132. Do NOT use fullscreen(). Import order: set
`gi.require_version` for Gtk 4.0, Gdk 4.0, Gsk 4.0, Graphene 1.0, Pango 1.0
as needed.

## Definition of done

- `pytest tests/test_layout.py` green, pristine output.
- `timeout 3 python3 src/ui.py` runs without traceback (exit code 124 is fine).
- Commit with a clear message.
