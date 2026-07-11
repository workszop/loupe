# Task 1: `src/portal.py` — PortalScreenCast + token persistence + --test-portal CLI

Read `.superpowers/sdd/interfaces.md` first — your class must match its
`PortalScreenCast` contract and the Global constraints section exactly.

## What to build

`src/portal.py`, self-contained module (GLib/Gio only, no GTK):

1. **Request/Response handshake helper.** Portal methods return immediately;
   results arrive via a `Response` signal on a Request object.
   - sender token: `bus.get_unique_name()` → strip leading `:`, replace `.` with `_`.
   - per-call `handle_token`: `f"loupe{n}"` from an incrementing counter.
   - expected request path:
     `/org/freedesktop/portal/desktop/request/{sender_token}/{handle_token}`.
   - `bus.signal_subscribe("org.freedesktop.portal.Desktop",
     "org.freedesktop.portal.Request", "Response", request_path, None,
     Gio.DBusSignalFlags.NO_MATCH_RULE ... use NONE, callback)` **before** the method
     call. Callback params variant is `(u, a{sv})` = (response_code, results).
     Unsubscribe after it fires.
   - Defensive: the portal method's return value is the actual request object
     path; if it differs from the precomputed path, re-subscribe on the
     returned path.
2. **Call sequence** on `org.freedesktop.portal.Desktop` /
   `/org/freedesktop/portal/desktop`, interface `org.freedesktop.portal.ScreenCast`,
   using `Gio.DBusConnection.call()` (async or sync-in-callback is your choice,
   but on_ready/on_error must fire from the main loop):
   - `CreateSession({handle_token, session_handle_token: "loupe_sess"})` →
     results contain `session_handle`.
   - `SelectSources(session_handle, options)` — options per Global constraints.
     Build variants explicitly: `GLib.Variant('a{sv}', {...})` with wrapped
     scalars (`GLib.Variant('u', 1)`, `GLib.Variant('b', False)`, `GLib.Variant('s', token)`).
     Omit the `restore_token` key entirely when no token is saved.
   - `Start(session_handle, "", {handle_token})` → results:
     `streams: a(ua{sv})` — take stream[0]: node_id + props dict (`size: (ii)`,
     `position: (ii)` — may be absent); `restore_token: s` (may be absent) —
     persist immediately, file mode 0600, parent dir created with default perms.
   - `OpenPipeWireRemote(session_handle, {})` via
     `call_with_unix_fd_list_sync` — return signature `(h)` is an INDEX into the
     returned `Gio.UnixFDList`; real fd = `fd_list.get(index)`. Then call
     `on_ready(node_id, fd, stream_props)`.
3. **Error handling:**
   - Response code 1 anywhere → `on_error(1, "cancelled by user")`.
   - Response code 2 while a restore_token WAS supplied → delete the token
     file and retry the whole flow ONCE from CreateSession without a token
     (do not call on_error for the first failure).
   - Response code 2 otherwise / D-Bus exception → `on_error(2, message)`.
4. **`close()`**: call `Close` on the session object path, interface
   `org.freedesktop.portal.Session`; swallow all errors; idempotent.
5. **`--test-portal` entry point**: `python3 src/portal.py --test-portal` runs a
   GLib.MainLoop, does the full flow, prints
   `node_id=<n> fd=<n> props=<dict>` on success (then closes session and
   exits 0), prints error and exits 1 on failure, exits 0 with message
   `cancelled` on user cancel.

## Environment

Pop!_OS 24.04, COSMIC. `xdg-desktop-portal-cosmic` is RUNNING and ScreenCast is
version 5 (restore_token supported; verified). You may run
`busctl --user introspect org.freedesktop.portal.Desktop /org/freedesktop/portal/desktop`
to check signatures.

**You may run `--test-portal` yourself:** the FIRST run pops a system share
dialog which nobody will click — so run it with a timeout
(`timeout 8 python3 src/portal.py --test-portal`); a clean timeout while the
dialog is up counts as success for the interactive leg (say so in your report).
If it errors before the dialog, that's a real bug — fix it.
Do NOT loop retrying it (each run pops a dialog).

## Tests (pytest, `tests/test_portal.py`)

Unit-test the pure parts without D-Bus (TDD for these):
- sender-token mangling (`:1.42` → `1_42`)
- request-path construction
- token persistence: save/load/delete round-trip in a tmp_path; file mode 0600;
  load returns None when file missing
- SelectSources options dict: token key omitted when None, present when saved
  (factor options-building into a testable function returning the dict of
  GLib.Variants or plain values)

Do NOT try to mock the whole D-Bus flow — the D-Bus leg is covered by
--test-portal manual verification at integration time.

## Definition of done

- pytest green, output pristine.
- `python3 -c "import sys; sys.path.insert(0,'src'); import portal"` works.
- `timeout 8 python3 src/portal.py --test-portal` reaches the dialog (or
  succeeds silently if a token exists) without traceback.
- Commit with a clear message.
