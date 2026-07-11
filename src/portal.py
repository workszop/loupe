"""xdg-desktop-portal ScreenCast client + restore-token persistence.

Implements the CreateSession -> SelectSources -> Start -> OpenPipeWireRemote
handshake against org.freedesktop.portal.Desktop, plus persistence of the
restore_token so a later run can skip the source-picker dialog.

No GTK imports; GLib/Gio only.
"""

import os
import sys

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")

from gi.repository import Gio, GLib

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"

DEFAULT_TOKEN_PATH = os.path.join(
    os.path.expanduser("~"), ".local", "state", "loupe", "restore_token"
)


# --- pure helpers (unit-testable without D-Bus) ---


def mangle_sender(unique_name: str) -> str:
    """':1.42' -> '1_42' (portal sender token convention)."""
    return unique_name.lstrip(":").replace(".", "_")


def request_path(sender_token: str, handle_token: str) -> str:
    return f"/org/freedesktop/portal/desktop/request/{sender_token}/{handle_token}"


def save_token(token_path: str, token: str) -> None:
    parent = os.path.dirname(token_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(token_path, 0o600)


def load_token(token_path: str) -> str | None:
    try:
        with open(token_path, "r", encoding="utf-8") as f:
            data = f.read().strip()
    except FileNotFoundError:
        return None
    return data or None


def delete_token(token_path: str) -> None:
    try:
        os.remove(token_path)
    except FileNotFoundError:
        pass


def build_select_sources_options(restore_token: str | None) -> dict:
    """Options dict for SelectSources, per Global constraints:
    types=1 (MONITOR), multiple=false, cursor_mode=1 (HIDDEN), persist_mode=2,
    restore_token only when one was saved.
    """
    options = {
        "types": GLib.Variant("u", 1),
        "multiple": GLib.Variant("b", False),
        "cursor_mode": GLib.Variant("u", 1),
        "persist_mode": GLib.Variant("u", 2),
    }
    if restore_token is not None:
        options["restore_token"] = GLib.Variant("s", restore_token)
    return options


class PortalScreenCast:
    def __init__(self, token_path: str | None = None):
        self._token_path = token_path or DEFAULT_TOKEN_PATH
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._sender_token = mangle_sender(self._bus.get_unique_name())
        self._handle_counter = 0
        self._session_handle = None
        self._closed = False
        self._retried_without_token = False

    # -- request/response plumbing --

    def _next_handle_token(self) -> str:
        self._handle_counter += 1
        return f"loupe{self._handle_counter}"

    def _subscribe_response(self, path, on_response):
        """Subscribe to Response on `path`; on_response(code, results) fires once,
        then this unsubscribes itself."""
        sub_id_holder = []

        def callback(connection, sender_name, object_path, interface_name,
                     signal_name, parameters, *user_data):
            code, results = parameters.unpack()
            if sub_id_holder:
                self._bus.signal_unsubscribe(sub_id_holder[0])
            on_response(code, results)

        sub_id = self._bus.signal_subscribe(
            PORTAL_BUS_NAME,
            REQUEST_IFACE,
            "Response",
            path,
            None,
            Gio.DBusSignalFlags.NONE,
            callback,
        )
        sub_id_holder.append(sub_id)
        return sub_id

    def _handle_common_failure(self, code, results, restore_token, step_name,
                                on_ready, on_error) -> bool:
        """Shared response-code handling for CreateSession/SelectSources/Start.

        Returns True if the response was handled as a failure (caller should
        stop); False if code == 0 (success, caller proceeds).
        """
        if code == 1:
            on_error(1, "cancelled by user")
            return True
        if code == 0:
            return False
        # code == 2, or any other non-zero code: treat as portal error, but
        # if a restore_token was in play, retry the whole flow once without it.
        if restore_token is not None and not self._retried_without_token:
            self._retried_without_token = True
            delete_token(self._token_path)
            self._close_session_quietly()
            self._start_flow(None, on_ready, on_error)
            return True
        message = results.get("_dbus_error", f"{step_name} failed: code={code}")
        on_error(2, message)
        return True

    # -- public API --

    def start(self, on_ready, on_error) -> None:
        used_token = load_token(self._token_path)
        self._start_flow(used_token, on_ready, on_error)

    def _start_flow(self, restore_token, on_ready, on_error):
        def on_create_session_response(code, results):
            if self._handle_common_failure(code, results, restore_token,
                                             "CreateSession", on_ready, on_error):
                return
            self._session_handle = results["session_handle"]
            self._select_sources(restore_token, on_ready, on_error)

        handle_token = self._next_handle_token()
        args = GLib.Variant(
            "(a{sv})",
            (
                {
                    "handle_token": GLib.Variant("s", handle_token),
                    "session_handle_token": GLib.Variant("s", "loupe_sess"),
                },
            ),
        )
        self._invoke(SCREENCAST_IFACE, "CreateSession", args, handle_token,
                      on_create_session_response)

    def _select_sources(self, restore_token, on_ready, on_error):
        options = build_select_sources_options(restore_token)
        handle_token = self._next_handle_token()
        options["handle_token"] = GLib.Variant("s", handle_token)
        args = GLib.Variant("(oa{sv})", (self._session_handle, options))

        def on_response(code, results):
            if self._handle_common_failure(code, results, restore_token,
                                            "SelectSources", on_ready, on_error):
                return
            self._start_session(restore_token, on_ready, on_error)

        self._invoke(SCREENCAST_IFACE, "SelectSources", args, handle_token, on_response)

    def _start_session(self, restore_token, on_ready, on_error):
        handle_token = self._next_handle_token()
        args = GLib.Variant("(osa{sv})", (
            self._session_handle,
            "",
            {"handle_token": GLib.Variant("s", handle_token)},
        ))

        def on_response(code, results):
            if self._handle_common_failure(code, results, restore_token,
                                            "Start", on_ready, on_error):
                return

            streams = results["streams"]
            node_id, stream_props_raw = streams[0]
            stream_props = {}
            if "size" in stream_props_raw:
                stream_props["size"] = stream_props_raw["size"]
            if "position" in stream_props_raw:
                stream_props["position"] = stream_props_raw["position"]

            new_token = results.get("restore_token")
            if new_token:
                save_token(self._token_path, new_token)

            self._open_pipewire_remote(node_id, stream_props, on_ready, on_error)

        self._invoke(SCREENCAST_IFACE, "Start", args, handle_token, on_response)

    def _open_pipewire_remote(self, node_id, stream_props, on_ready, on_error):
        args = GLib.Variant("(oa{sv})", (self._session_handle, {}))
        try:
            reply, fd_list = self._bus.call_with_unix_fd_list_sync(
                PORTAL_BUS_NAME,
                PORTAL_OBJECT_PATH,
                SCREENCAST_IFACE,
                "OpenPipeWireRemote",
                args,
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
                None,
            )
        except GLib.Error as e:
            on_error(2, f"OpenPipeWireRemote failed: {e}")
            return

        (fd_index,) = reply.unpack()
        fd = fd_list.get(fd_index)
        on_ready(node_id, fd, stream_props)

    def _invoke(self, iface, method_name, args, handle_token, on_response):
        expected_path = request_path(self._sender_token, handle_token)
        state = {"responded": False}

        def wrapped(code, results):
            if state["responded"]:
                return
            state["responded"] = True
            on_response(code, results)

        sub_id = self._subscribe_response(expected_path, wrapped)

        def on_call_done(source, result):
            try:
                reply = self._bus.call_finish(result)
            except GLib.Error as e:
                self._bus.signal_unsubscribe(sub_id)
                if not state["responded"]:
                    state["responded"] = True
                    on_response(-1, {"_dbus_error": str(e)})
                return
            actual_path = reply.unpack()[0]
            if actual_path != expected_path:
                self._bus.signal_unsubscribe(sub_id)
                self._subscribe_response(actual_path, wrapped)

        self._bus.call(
            PORTAL_BUS_NAME,
            PORTAL_OBJECT_PATH,
            iface,
            method_name,
            args,
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            on_call_done,
        )

    def _close_session_quietly(self) -> None:
        """Close the current session object (if any) without marking this
        PortalScreenCast as permanently closed — used when retrying the flow
        after a stale-token failure."""
        if self._session_handle is None:
            return
        try:
            self._bus.call_sync(
                PORTAL_BUS_NAME,
                self._session_handle,
                SESSION_IFACE,
                "Close",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except GLib.Error:
            pass
        self._session_handle = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_session_quietly()


def _run_test_portal():
    loop = GLib.MainLoop()
    result = {"code": 0}

    portal_screencast = PortalScreenCast()

    def on_ready(node_id, fd, stream_props):
        print(f"node_id={node_id} fd={fd} props={stream_props}")
        portal_screencast.close()
        loop.quit()

    def on_error(code, message):
        if code == 1:
            print("cancelled")
            result["code"] = 0
        else:
            print(f"error: {message}", file=sys.stderr)
            result["code"] = 1
        loop.quit()

    portal_screencast.start(on_ready, on_error)
    loop.run()
    sys.exit(result["code"])


if __name__ == "__main__":
    if "--test-portal" in sys.argv:
        _run_test_portal()
