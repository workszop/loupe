# Module interface contract (binding for all tasks)

All modules: Python 3.12, PyGObject. `gi.require_version` before repository
imports. No third-party deps beyond PyGObject/GStreamer girs. Each module must
be importable on its own (no cross-imports between src modules except ui.py may
NOT import framesource/portal — it receives objects via constructor).

```python
# src/portal.py — NO GTK imports (GLib/Gio only)
class PortalScreenCast:
    def __init__(self, token_path: str | None = None): ...
        # default token path: ~/.local/state/loupe/restore_token (dir created, file mode 0600)
    def start(self, on_ready, on_error) -> None: ...
        # async, requires a running GLib main loop
        # on_ready(node_id: int, fd: int, stream_props: dict)
        #   stream_props: {'size': (w, h), 'position': (x, y)} — keys present only if portal sent them
        # on_error(code: int, message: str)  # code 1 = user cancelled dialog, 2 = other portal error
    def close(self) -> None: ...  # idempotent, safe before/after start

# src/framesource.py — NO GTK imports except Gdk/GLib (no Gtk widgets)
class FrameSource:
    def __init__(self, fd: int, node_id: int, on_frame, on_error,
                 pipeline_override: str | None = None): ...
        # on_frame() — zero-arg, invoked on the GLib main loop, coalesced
        #   (never more than one pending idle regardless of frame rate)
        # on_error(message: str) — main loop, pipeline error or 5s first-frame watchdog
        # pipeline_override — full gst-launch string for testing (e.g. videotestsrc);
        #   when None, build the pipewiresrc pipeline from fd/node_id
    def start(self) -> None: ...
    def get_texture(self):  # -> Gdk.MemoryTexture | None — GTK/main thread only, cached per frame
    def get_latest_frame(self):  # -> tuple[bytes, int, int, int, str] | None
        # (data, width, height, stride, gst_format) e.g. ('...', 1920, 1200, 7680, 'BGRx')
        # for the calibration scan; returns the same bytes the texture is built from
    def stop(self) -> None: ...  # idempotent

# src/ui.py — pure math + widgets. MUST NOT import portal/framesource.
LENS_W, LENS_H = 480, 320
MARGIN = 20            # min gap between lens outer edge and source rect
RADIUS = 14            # lens corner radius
BORDER = 2.0           # lens border width (part of lens outer rect!)
ZOOM_MIN, ZOOM_MAX, ZOOM_STEP = 1.5, 8.0, 1.25
ZOOM_DEFAULT = 2.5

@dataclass(frozen=True)
class Layout:
    src: tuple[float, float, float, float]   # sx, sy, sw, sh — window coords
    lens: tuple[float, float, float, float]  # lx, ly, LENS_W, LENS_H — window coords

def compute_layout(cx: float, cy: float, zoom: float, win_w: int, win_h: int) -> Layout: ...
    # HARD GUARANTEE: lens outer rect (lens expanded by BORDER on all sides)
    # never intersects src rect, for every cx,cy inside the window and every
    # zoom in [ZOOM_MIN, ZOOM_MAX], for win sizes >= 1280x720.

class LoupeWindow(Gtk.ApplicationWindow):
    def __init__(self, *, application, frame_source, on_quit): ...
        # frame_source: object with get_texture() -> Gdk.Texture | None
        # on_quit: zero-arg callable (Esc / click / Ctrl+Q) — window does NOT quit the app itself
    def set_frame_offset(self, ox: float, oy: float) -> None: ...
        # window coord (x,y) corresponds to frame coord (x+ox, y+oy).
        # Until called: calibration mode — draw ONLY a 16x16 solid #FF00FF square
        # at window (0,0); no lens.
    def notify_frame(self) -> None: ...   # queue redraw (wired to FrameSource.on_frame)

# src/lifecycle.py — GLib only, no GTK
PIDFILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "loupe.pid")
def acquire_pidfile_or_toggle() -> bool: ...
    # True  = we own the pidfile now, proceed
    # False = a live loupe instance was found and SIGTERMed (toggle-off); caller exits 0
    # stale pidfile (dead pid, or pid whose /proc/<pid>/cmdline lacks 'loupe') is replaced
def release_pidfile() -> None: ...       # idempotent, only removes file if it holds our pid
def install_signal_handlers(cleanup_cb) -> None: ...  # SIGTERM+SIGINT via GLib.unix_signal_add
def fail(message: str, hint: str = "") -> None: ...
    # stderr always; best-effort desktop notification (Gio.Application may not exist yet —
    #   use notify-send subprocess fallback); does NOT exit
```

## Global constraints (verbatim from plan)

- Live magnifier; rectangular lens 480x320; zoom Ctrl+= / Ctrl+plus / plus zoom
  in, Ctrl+- / minus zoom out, scroll wheel zooms; zoom x1.25 steps clamped to
  [1.5, 8.0]; Esc or click exits; brief "N.NNx" OSD on zoom change.
- Maximized undecorated transparent window (NOT fullscreen — COSMIC paints
  fullscreen opaque). Transparency: display-level Gtk.CssProvider,
  load_from_string, selector `window, .background { background: transparent; }`.
- Lens outer rect must never overlap its source rect (screencast feedback loop).
- Portal: SelectSources options types=1 (MONITOR), multiple=false,
  cursor_mode=1 (HIDDEN), persist_mode=2, restore_token only when saved.
  Subscribe to Request Response signal BEFORE the method call. Persist the
  new restore_token from Start's response every run (tokens rotate).
- OpenPipeWireRemote via call_with_unix_fd_list_sync (returns fd index into fd list).
- Pipeline: pipewiresrc fd=F path=N do-timestamp=true keepalive-time=1000 !
  videoconvert ! video/x-raw,format=BGRx ! appsink emit-signals=true
  max-buffers=2 drop=true sync=false. GstApp gir may be MISSING on the target
  machine: try gi GstApp, fall back to sink.emit("pull-sample"). Read stride
  from GstVideo.buffer_get_video_meta when available (GstVideo gir may also be
  missing — wrap in try/except, fallback width*4).
- App id dev.andrzey.loupe, Gio.ApplicationFlags.NON_UNIQUE.
