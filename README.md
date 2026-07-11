# loupe — live screen magnifier for COSMIC/Wayland

Press Super+Z → a magnified rectangular lens follows your cursor over a live
screencast of the desktop. Ctrl+= / Ctrl+- (or scroll) changes zoom. Esc, a
click, or Super+Z again dismisses it.

Dev repo. The deliverable is a single self-contained file built from `src/`
and installed to `~/bin/loupe.py`.

## Architecture

- `src/portal.py` — `PortalScreenCast`: xdg-desktop-portal ScreenCast D-Bus flow
  (CreateSession → SelectSources → Start → OpenPipeWireRemote), restore-token
  persistence in `~/.local/state/loupe/restore_token`.
- `src/framesource.py` — `FrameSource`: GStreamer `pipewiresrc → videoconvert →
  appsink` pipeline; hands `Gdk.MemoryTexture` frames to the UI.
- `src/ui.py` — `LoupeWindow` (maximized undecorated transparent GTK4 window that
  captures pointer/keyboard) + pure `compute_layout()` lens-placement math.
- `src/lifecycle.py` — pidfile toggle, signal handling, error reporting.
- `loupe.py` — generated single-file build (do not edit directly).

## Key platform facts (verified by spike on Pop!_OS 24.04 / COSMIC)

- COSMIC renders **fullscreen** surfaces with an opaque backdrop; **maximized**
  undecorated windows honor `window, .background { background: transparent; }`
  (via `Gtk.CssProvider.load_from_string` at display level). So: maximized, not
  fullscreen.
- The maximized window does not cover the panel strip and Wayland gives no
  window position → a one-time magenta-marker calibration in the first frame
  maps window coords → frame coords.
- ScreenCast portal v5: restore_token supported; cursor modes HIDDEN|EMBEDDED
  only → cursor position comes from our own window's motion events.
- The lens must NEVER overlap its source region: the screencast captures our
  own overlay (infinite-mirror feedback otherwise).
