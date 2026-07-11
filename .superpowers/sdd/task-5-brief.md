# Task 5: Integration — src/main.py + single-file build → loupe.py

Read `.superpowers/sdd/interfaces.md` first. All four modules exist on main and
are review-approved: `src/portal.py`, `src/framesource.py`, `src/ui.py`,
`src/lifecycle.py`. Read each module's public surface (docstrings/signatures)
before wiring — do NOT modify them except where this brief explicitly says.

## Part A: `src/main.py` — application wiring

`main(argv)` flow:
1. `--test-portal` arg → delegate to portal module's existing test entry and return its exit code.
2. `lifecycle.acquire_pidfile_or_toggle()` → False = exit 0 (toggle-off path).
3. `framesource.have_pipewiresrc()` → False = `lifecycle.fail("GStreamer PipeWire plugin missing", hint="sudo apt install gstreamer1.0-pipewire gir1.2-gst-plugins-base-1.0")`, release pidfile, exit 1.
4. Build `Gtk.Application(application_id="dev.andrzey.loupe", flags=NON_UNIQUE)`; on activate:
   a. `lifecycle.install_signal_handlers(cleanup)`.
   b. `PortalScreenCast().start(on_ready, on_portal_error)`.
   c. on_ready(node_id, fd, props): create `FrameSource(fd, node_id, on_frame, on_fs_error)`, `start()` it, create `LoupeWindow(application=app, frame_source=fs, on_quit=cleanup)`, `present()`.
   d. on_portal_error: code 1 → silent cleanup + exit 0; else `fail(message)`, cleanup, exit 1.
   e. on_fs_error: `fail(message)`, cleanup, exit 1.
   f. `app.hold()` between portal start and window creation so the app doesn't exit early (window comes only after the portal handshake).
5. `cleanup()` idempotent: fs.stop() if created → portal.close() → lifecycle.release_pidfile() → app.quit(). ALL exit paths route through it (Esc/click/Ctrl+Q via on_quit, SIGTERM/SIGINT, portal cancel, pipeline error).

**Calibration** (the new logic — this maps window coords → frame coords):
- `on_frame()` while not calibrated: `fs.get_latest_frame()` → run
  `locate_marker(data, w, h, stride)`; if found at frame pixel (mx, my) →
  `window.set_frame_offset(mx, my)`, calibrated. Always also call
  `window.notify_frame()`.
- `locate_marker` = **pure function in main.py, developed with TDD**:
  find the top-left corner of the 16x16 solid magenta (#FF00FF) block the
  window draws at window (0,0) during calibration mode. Byte pattern per
  pixel is (FF, 00, FF) in channels 0..2 for ALL four possible formats
  (BGRx/RGBx/BGRA/RGBA — magenta is R/B symmetric, so format doesn't matter).
  Search region: rows 0..min(h,300), cols 0..min(w,400). A match requires the
  16-px horizontal run AND spot-checks on rows +4/+8/+15 (guards against
  coincidental magenta pixels elsewhere in that band). Return (x, y) or None.
- Fallback: if 30 frames or 2.5 s pass without a marker hit (count in
  on_frame / GLib timeout), assume `ox=0, oy=frame_h - window_height` and set
  that offset (log a warning to stderr).

Tests (`tests/test_main.py`, TDD): `locate_marker` on synthetic frames —
marker at (0,68) exact hit; marker at arbitrary (x,y) in region; no marker →
None; stride > w*4 (padded rows) handled; near-magenta noise (a 15px run, a
16px run with wrong row +8) rejected; marker partially outside search region →
None. No GTK/Gst needed — pure bytes.

## Part B: `tools/build.py` — single-file bundler

Deterministically produce `loupe.py` at repo root from the five src modules:
- Output layout: shebang `#!/usr/bin/env python3`, module docstring with usage
  + COSMIC shortcut note, ONE consolidated block of gi.require_version calls +
  imports (union of all modules', sibling imports dropped), then the source of
  lifecycle, portal, framesource, ui, main (in that order), each with its
  imports/shebang/docstring stripped and a `# ==== src/<name>.py ====` header,
  ending with `if __name__ == "__main__": sys.exit(main(sys.argv))`.
- Implementation approach is your choice (line filtering is fine) but it must
  be robust to the current files' actual contents — verify by running it.
- Note: `gi.require_version` must run before ANY `from gi.repository import`,
  and ui.py needs Gtk 4.0 while framesource needs Gst 1.0 and GstApp/GstVideo
  are conditional try/except imports — preserve that conditionality (keep the
  try/except import blocks inside the bundled framesource section if easier,
  as long as top consolidated imports don't import GstApp/GstVideo
  unconditionally).
- After writing: `chmod +x loupe.py`, `python3 -m py_compile loupe.py`, and a
  bundle smoke test that does NOT need pipewiresrc or a portal dialog:
  `python3 -c "import loupe"`? No — bundling as script, so instead run
  `timeout 3 python3 loupe.py` with a FAKE: add `--smoke` flag to main() that
  (a) skips the portal, (b) uses FrameSource with the videotestsrc override
  pipeline (LENS still works over the test pattern), (c) skips pidfile.
  `timeout 5 python3 loupe.py --smoke` must map the window without traceback
  (exit 124 OK). This --smoke flag is part of the spec, keep it (hidden,
  mentioned only in the module docstring).
- The build must be re-runnable (idempotent output for identical inputs).

## Part C: verification you must run

1. `pytest tests/` — full suite green (55 existing + your new main tests).
2. `python3 tools/build.py` then `python3 -m py_compile loupe.py`.
3. `timeout 5 python3 loupe.py --smoke` → window maps, no traceback (this
   shows a brief window with a test pattern + lens on the user's screen —
   run it at most twice).
4. `timeout 5 python3 loupe.py --smoke` ALSO verifies calibration fallback
   fires (videotestsrc frame has no magenta marker at typical patterns —
   stderr warning expected; assert visually in output text only).
5. Do NOT run bare `loupe.py` (needs pipewiresrc, not installed) and do NOT
   run `--test-portal` (pops a dialog).

pipewiresrc is NOT installed on this machine yet; the real end-to-end run
happens later and is out of your scope.

## Definition of done

- pytest green, pristine; build reproducible; smoke run clean.
- src/main.py + tools/build.py + generated loupe.py committed (yes, commit the
  generated artifact — it is the deliverable).
