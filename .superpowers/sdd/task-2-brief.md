# Task 2: `src/framesource.py` — FrameSource (GStreamer → Gdk textures)

Read `.superpowers/sdd/interfaces.md` first — your class must match its
`FrameSource` contract and the Global constraints section exactly.

## What to build

`src/framesource.py` (imports: gi → Gst, GLib, Gdk; NO Gtk):

1. **Pipeline.** When `pipeline_override` is None, build the pipewiresrc
   pipeline string from the Global constraints. With override, `Gst.parse_launch`
   the override string verbatim — it must end in an appsink named `sink`
   with the same appsink properties. (Use `Gst.parse_launch` for both; fetch the
   sink via `pipeline.get_by_name("sink")` — so the default string must name it
   `name=sink` too.)
2. **Startup check helper**: module-level `have_pipewiresrc() -> bool` using
   `Gst.ElementFactory.find("pipewiresrc")`. (The caller decides what to do —
   `FrameSource` itself must not exit the process.)
3. **new-sample callback** (runs on GStreamer streaming thread):
   - `sample = sink.pull_sample()` via GstApp if importable, else
     `sink.emit("pull-sample")` (this fallback MUST work — GstApp gir is
     currently missing on this machine).
   - Parse caps once per caps-change: width, height, format string.
   - Stride: `GstVideo.buffer_get_video_meta(buf)` when GstVideo importable
     and meta present → `meta.stride[0]`; else `width * 4`.
   - `data = buf.extract_dup(0, buf.get_size())`; under a `threading.Lock`,
     store `(data, w, h, stride, fmt)` and bump a frame counter.
   - Coalesced notify: an atomic `_pending` flag + `GLib.idle_add`; the idle
     resets the flag, then calls `on_frame()`. Never more than one queued idle.
   - Return `Gst.FlowReturn.OK`.
4. **`get_texture()`** (main thread): if frame counter changed since last
   build, construct `Gdk.MemoryTexture.new(w, h, fmt_map[fmt], GLib.Bytes.new(data), stride)`
   and cache it; else return cached. fmt_map: BGRx→B8G8R8X8, RGBx→R8G8B8X8,
   BGRA→B8G8R8A8, RGBA→R8G8B8A8. Unknown format → call on_error once, return None.
5. **`get_latest_frame()`**: return the raw tuple under the lock (see contract).
6. **Bus watch**: `add_signal_watch()`, on ERROR → `on_error(f"{err}: {dbg}")`
   (marshalled to main loop — bus signal watch already delivers on main loop).
7. **Watchdog**: after `start()`, `GLib.timeout_add_seconds(5, ...)`: if no
   frame received yet → `on_error("no frames after 5s")`. Cancel/ignore once
   the first frame arrives or after stop().
8. **`stop()`**: set state NULL, remove bus watch, idempotent.
9. **Caps-negotiation fallback**: if the bus reports a not-negotiated error on
   the DEFAULT pipeline, rebuild once without the `format=BGRx` restriction
   (keep `video/x-raw`) and restart; the fmt_map handles whatever arrives. Only
   one retry; second failure → on_error.

## Environment

`pipewiresrc` is NOT installed on this machine yet (it will be at integration
time) — so all your live testing uses `pipeline_override` with `videotestsrc`:
`videotestsrc is-live=true ! videoconvert ! video/x-raw,format=BGRx ! appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false`
GstApp gir is missing here — your emit("pull-sample") fallback is what runs.

## Tests (pytest, `tests/test_framesource.py`) — these RUN gstreamer for real

- fixture: GLib.MainLoop + FrameSource with the videotestsrc override.
- on_frame fires within 2s; after it, `get_latest_frame()` returns w=320,h=240
  (videotestsrc default) or whatever caps you pin in the override — pin them
  (`width=320,height=240`) so the assert is exact; stride >= w*4; fmt == 'BGRx'.
- `get_texture()` returns a `Gdk.MemoryTexture` with matching size; calling it
  twice without a new frame returns the SAME object (cache hit).
- coalescing: after the first on_frame, sleep the main loop 200ms via a
  timeout, count on_frame invocations — must be >=1 but the count must equal
  the number of idles fired, and at no point may two idles be pending (assert
  via the _pending flag semantics: expose it as `_pending` for the test or
  count callback invocations vs frame counter — callbacks <= frames).
- stop() twice → no error; get_texture() after stop returns last frame or None
  without crashing.
- watchdog: FrameSource with override `videotestsrc num-buffers=0 ! ...`? that
  EOSes instead. Simpler: override with a valid pipeline but never set PLAYING —
  not possible via public API. Instead: make the watchdog delay a constructor
  kwarg `watchdog_sec=5` and test with `watchdog_sec=1` and an override
  pipeline `fakesrc ! fakesink`? that has no appsink named sink → constructor
  should raise ValueError (test that too). For the watchdog itself use
  `videotestsrc is-live=true ! ... appsink` but with the pipeline forced to
  PAUSED: add test-only method or just trust the 5s path — acceptable to test
  watchdog with `appsink` never receiving data using
  `videotestsrc is-live=true pattern=ball num-buffers=0`? If num-buffers=0
  errors immediately that also exercises on_error. Choose ONE workable
  approach; a watchdog test that is flaky is worse than none — if you cannot
  make it deterministic, document it as manually-verified and move on.

Headless note: these tests need no display (no Gtk window), but
`Gdk.MemoryTexture` needs only Gdk import — verify `gi.require_version("Gdk","4.0")`
import works without a display connection (it does; Gdk.Display is not used).
Run tests with WAYLAND_DISPLAY unset if needed to prove it.

## Definition of done

- pytest green, output pristine (no GStreamer warnings — set GST_DEBUG=1 in
  tests if needed to silence expected noise, but investigate real warnings).
- Commit with a clear message.
