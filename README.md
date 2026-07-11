# loupe — freeze-frame screen magnifier for COSMIC/Wayland

Press Super+Z → the screen freezes and a magnifier lens appears centered on
your cursor. Move the mouse to read small text/UI anywhere on the frozen
screen; the lens follows the cursor and slides off the screen edge rather than
jumping. Ctrl+= / Ctrl+- (or scroll) changes zoom. **Left-click** to click the
target under the crosshair on the live app underneath (the loupe closes and
fires the real click via /dev/uinput). Esc, right-click, or Super+Z cancel.

Built for **reading**, not interacting: the magnified image is a snapshot taken
at launch, so it needs no screen-share permission and starts instantly.
Re-launch to refresh.

## Architecture

Single self-contained file, generated from `src/` into `~/bin/loupe.py`.

- `src/capture.py` — `grab_screenshot()`: one silent `cosmic-screenshot` into a
  `Gdk.Texture`.
- `src/ui.py` — `compute_layout()` (lens centered on cursor, no clamping) plus
  `LoupeWindow`/`LensWidget`: a fullscreen opaque window that draws the frozen
  screenshot at 1:1 and a magnifier lens centered on the cursor.
- `src/click.py` — `VirtualPointer`: a uinput virtual pointer that clicks at the
  current position (the pointer is already where the user aimed, so no
  positioning needed).
- `src/lifecycle.py` — pidfile toggle, signal handling, error reporting.
- `src/main.py` — grab snapshot → maximized window → present; wires click-through.
- `loupe.py` — generated single-file build (do not edit directly).

## Build & install

```
python3 tools/build.py        # regenerate loupe.py from src/
cp loupe.py ~/bin/loupe.py
```

Bind Super+Z to `~/bin/loupe.py` in COSMIC Settings → Input → Keyboard →
Custom Shortcuts (a second Super+Z toggles it off).

## Key facts

- Maximized + opaque (NOT fullscreen — COSMIC animates fullscreen enter/exit
  with a black transition). The window covers the work area; the full-screen
  screenshot is aligned to it by the panel-height offset (tex_h − win_h).
- Freeze-frame means the lens can sit directly on the cursor with no screencast
  feedback loop — the reason a live version had to offset the lens beside the
  cursor — and click-through can hide the frozen overlay then click the live app.
- Click-through needs write access to /dev/uinput (the logged-in user has it via
  a systemd-logind seat ACL). Degrades to plain dismiss if unavailable.
- Assumes 100% display scale (window ≈ screenshot, 1:1 minus panel). HiDPI would
  need a scale factor in the lens math.
- Lens is 1440×480 at 2.5× default zoom (1.5×–8× range).
