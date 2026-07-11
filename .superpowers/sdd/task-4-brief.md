# Task 4: `src/lifecycle.py` — pidfile toggle, signals, error reporting

Read `.superpowers/sdd/interfaces.md` first — the four functions and PIDFILE
constant are the binding contract.

## What to build

`src/lifecycle.py` (imports: os, sys, signal, subprocess, gi → GLib only):

1. `acquire_pidfile_or_toggle() -> bool`
   - No pidfile → write our pid, return True.
   - Pidfile exists → read pid (int parse failure = stale). Check liveness with
     `os.kill(pid, 0)` (ESRCH = dead = stale; EPERM counts as alive). If alive,
     read `/proc/<pid>/cmdline` (bytes, NUL-separated) and require b"loupe" in
     it — otherwise the pid was recycled by another process = stale.
   - Live loupe → `os.kill(pid, signal.SIGTERM)`, return False (caller exits —
     this IS the toggle-off path).
   - Stale → replace with our pid, return True.
   - Pidfile writes: write pid + newline, no locking needed (single user).
2. `release_pidfile()` — remove PIDFILE only if it exists AND contains our own
   pid (do not delete a newer instance's file); swallow all OSErrors; idempotent.
3. `install_signal_handlers(cleanup_cb)` — `GLib.unix_signal_add(
   GLib.PRIORITY_DEFAULT, signal.SIGTERM, handler)` and same for SIGINT; the
   handler calls `cleanup_cb()` and returns `GLib.SOURCE_REMOVE`.
4. `fail(message, hint="")` — always print to stderr (`loupe: {message}`,
   hint on second line if given); best-effort desktop notification via
   `subprocess.run(["notify-send", "-a", "loupe", "loupe", message + ("\n" + hint if hint else "")], timeout=2)`
   inside try/except (notify-send may be missing); never raises, never exits.

## Tests (pytest, `tests/test_lifecycle.py`) — TDD; make PIDFILE patchable

Make the pidfile path a module attribute read at call time so tests can
monkeypatch it to a tmp_path file.

- fresh acquire: no file → True, file contains our pid.
- stale (dead pid): write a pid from `os.spawn`-and-waited or just an absurd
  unused pid like 2**22-ish that doesn't exist (verify with os.kill ESRCH first
  in the test) → acquire returns True, file now ours.
- stale (recycled pid): write pid of a live NON-loupe process — spawn
  `sleep 5` via subprocess, use its pid → acquire returns True (cmdline lacks
  "loupe"), sleeper NOT killed (poll() is None after).
- toggle: spawn a real dummy process whose cmdline contains "loupe":
  `subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "loupe-dummy"])`
  — argv shows in /proc/pid/cmdline → acquire returns False and the dummy
  receives SIGTERM (returncode == -15 after wait with timeout).
- release: only deletes own-pid file; leaves foreign-pid file alone; idempotent
  when file missing.
- fail(): capsys shows message + hint on stderr; monkeypatch subprocess.run to
  raise FileNotFoundError → still no exception.
- install_signal_handlers: register with a flag-setting callback, send
  ourselves SIGTERM inside a running GLib.MainLoop iteration, assert callback
  ran and loop can quit. (GLib main loop must be iterating for unix_signal_add
  to dispatch — use GLib.MainContext.default().iteration or a short MainLoop
  with a timeout quit.)

## Definition of done

- pytest green, pristine output, no orphan processes left behind (all spawned
  test processes waited/killed in fixtures).
- Commit with a clear message.
