#!/usr/bin/env python3
"""loupe entry point: wires portal/framesource/ui/lifecycle together.

Flow: acquire the single-instance pidfile (or toggle off a running instance),
check pipewiresrc is available, open a Gtk.Application, drive the portal
handshake on activate, and on the portal's first stream build a FrameSource +
LoupeWindow. A short calibration pass (`locate_marker`) maps window
coordinates to frame coordinates by locating a magenta marker the window
itself draws in its first frames.

Usage: loupe.py [--test-portal] [--smoke]
  --test-portal  run only the portal handshake (src/portal.py entry point)
                 and print node_id/fd/props; pops the portal consent dialog.
  --smoke        internal: skip the portal, run a synthetic videotestsrc
                 pipeline instead, and skip the pidfile toggle. Used by the
                 single-file build's smoke test; not for interactive use.

On COSMIC, bind Super+Z to run this script; a second Super+Z toggles it off.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import signal
import subprocess
import sys
import threading

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import GLib, Gdk, Gio, Graphene, Gsk, Gst, Gtk, Pango

# ==== src/lifecycle.py ====
PIDFILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "loupe.pid")


def _write_own_pid():
    with open(PIDFILE, "w") as f:
        f.write(f"{os.getpid()}\n")


def _is_stale(pid):
    """Return True if pid is dead or alive-but-not-loupe (recycled)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True  # ESRCH: dead
    except PermissionError:
        pass  # EPERM: alive, owned by someone else — still counts as alive

    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read()
    except OSError:
        return True  # process vanished between kill(0) and cmdline read

    return b"loupe" not in cmdline


def acquire_pidfile_or_toggle() -> bool:
    if not os.path.exists(PIDFILE):
        _write_own_pid()
        return True

    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        _write_own_pid()
        return True

    if _is_stale(pid):
        _write_own_pid()
        return True

    # Live loupe instance found — toggle it off.
    os.kill(pid, signal.SIGTERM)
    return False


def release_pidfile() -> None:
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return

    if pid != os.getpid():
        return

    try:
        os.remove(PIDFILE)
    except OSError:
        pass


def install_signal_handlers(cleanup_cb) -> None:
    def handler(*_args):
        cleanup_cb()
        return GLib.SOURCE_REMOVE

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, handler)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, handler)


def fail(message: str, hint: str = "") -> None:
    text = f"loupe: {message}"
    if hint:
        text += f"\n{hint}"
    print(text, file=sys.stderr)

    try:
        body = message + (f"\n{hint}" if hint else "")
        subprocess.run(
            ["notify-send", "-a", "loupe", "loupe", body], timeout=2
        )
    except Exception:
        pass


# ==== src/portal.py ====
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


DBUS_ERROR_KEY = "_dbus_error"


def should_retry_stale_token(step: str, code: int, token_supplied: bool,
                              already_retried: bool) -> bool:
    """Whether a failure response should trigger the stale-token retry.

    Per the spec, ONLY the SelectSources response carries a restore_token,
    so the retry (delete token + restart flow once from CreateSession
    without a token) applies exclusively there: code == 2, a restore_token
    was supplied, and we haven't already retried once.
    """
    return (
        step == "SelectSources"
        and code == 2
        and token_supplied
        and not already_retried
    )


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
        if DBUS_ERROR_KEY in results:
            # Transport-level failure, not a portal response: never retries
            # and never touches the persisted token.
            on_error(2, results[DBUS_ERROR_KEY])
            return True
        # code == 2, or any other non-zero code: treat as portal error, but
        # the stale-token retry applies only to the SelectSources response
        # (the only call that sends restore_token).
        if should_retry_stale_token(step_name, code, restore_token is not None,
                                     self._retried_without_token):
            self._retried_without_token = True
            delete_token(self._token_path)
            self._close_session_quietly()
            self._start_flow(None, on_ready, on_error)
            return True
        message = f"{step_name} failed: code={code}"
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


# ==== src/framesource.py ====
try:
    gi.require_version("GstApp", "1.0")
    from gi.repository import GstApp  # noqa: F401
    _HAVE_GSTAPP = True
except (ValueError, ImportError):
    _HAVE_GSTAPP = False

try:
    gi.require_version("GstVideo", "1.0")
    from gi.repository import GstVideo
    _HAVE_GSTVIDEO = True
except (ValueError, ImportError):
    _HAVE_GSTVIDEO = False

if not Gst.is_initialized():
    Gst.init(None)

# gst video format string -> Gdk.MemoryFormat
_FMT_MAP = {
    "BGRx": Gdk.MemoryFormat.B8G8R8X8,
    "RGBx": Gdk.MemoryFormat.R8G8B8X8,
    "BGRA": Gdk.MemoryFormat.B8G8R8A8,
    "RGBA": Gdk.MemoryFormat.R8G8B8A8,
}


def have_pipewiresrc() -> bool:
    """Whether the pipewiresrc GStreamer element is available on this system."""
    return Gst.ElementFactory.find("pipewiresrc") is not None


def _default_pipeline_string(fd: int, node_id: int, *, restrict_format: bool) -> str:
    caps = "video/x-raw,format=BGRx" if restrict_format else "video/x-raw"
    return (
        f"pipewiresrc fd={fd} path={node_id} do-timestamp=true keepalive-time=1000 "
        f"! videoconvert ! {caps} "
        f"! appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
    )


def _is_not_negotiated(err: GLib.Error, debug: str | None) -> bool:
    """Best-effort detection of a caps-negotiation failure from a Gst bus ERROR."""
    if debug and "not-negotiated" in debug.lower():
        return True
    if err.matches(Gst.core_error_quark(), Gst.CoreError.NEGOTIATION):
        return True
    return False


class FrameSource:
    def __init__(
        self,
        fd: int,
        node_id: int,
        on_frame,
        on_error,
        pipeline_override: str | None = None,
        watchdog_sec: float = 5,
    ):
        self._fd = fd
        self._node_id = node_id
        self._on_frame = on_frame
        self._on_error = on_error
        self._pipeline_override = pipeline_override
        self._watchdog_sec = watchdog_sec

        self._lock = threading.Lock()
        self._latest = None  # (data, w, h, stride, fmt)
        self._frame_counter = 0

        self._pending = False

        self._cached_texture = None
        self._cached_counter = -1

        self._caps_str = None
        self._caps_w = None
        self._caps_h = None
        self._caps_fmt = None

        self._pipeline = None
        self._sink = None
        self._bus = None

        self._watchdog_id = None
        self._stopped = False
        self._retried_caps = False
        self._error_reported = False

        self._build_pipeline(restrict_format=True)

    # -- pipeline construction ------------------------------------------------

    def _build_pipeline(self, *, restrict_format: bool) -> None:
        if self._pipeline_override is not None:
            pipeline_str = self._pipeline_override
        else:
            pipeline_str = _default_pipeline_string(
                self._fd, self._node_id, restrict_format=restrict_format
            )

        pipeline = Gst.parse_launch(pipeline_str)
        sink = pipeline.get_by_name("sink")
        if sink is None:
            raise ValueError(
                "pipeline_override must contain an appsink named 'sink'"
            )

        self._pipeline = pipeline
        self._sink = sink
        sink.connect("new-sample", self._on_new_sample)

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)
        self._bus = bus

    def _teardown_pipeline(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
        if self._bus is not None:
            self._bus.remove_signal_watch()
        self._pipeline = None
        self._sink = None
        self._bus = None

    # -- public API -------------------------------------------------------

    def start(self) -> None:
        self._stopped = False
        self._pipeline.set_state(Gst.State.PLAYING)
        self._arm_watchdog()

    def _arm_watchdog(self) -> None:
        self._cancel_watchdog()
        self._watchdog_id = GLib.timeout_add_seconds(
            int(self._watchdog_sec), self._on_watchdog_timeout
        )

    def _cancel_watchdog(self) -> None:
        if self._watchdog_id is not None:
            GLib.source_remove(self._watchdog_id)
            self._watchdog_id = None

    def _on_watchdog_timeout(self) -> bool:
        self._watchdog_id = None
        if not self._stopped and self._frame_counter == 0:
            self._on_error(f"no frames after {self._watchdog_sec}s")
        return GLib.SOURCE_REMOVE

    def get_texture(self):
        with self._lock:
            latest = self._latest
            counter = self._frame_counter
        if latest is None:
            return self._cached_texture
        if counter == self._cached_counter:
            return self._cached_texture

        data, w, h, stride, fmt = latest
        memfmt = _FMT_MAP.get(fmt)
        if memfmt is None:
            if not self._error_reported:
                self._error_reported = True
                self._on_error(f"unsupported video format: {fmt}")
            return None

        texture = Gdk.MemoryTexture.new(w, h, memfmt, GLib.Bytes.new(data), stride)
        self._cached_texture = texture
        self._cached_counter = counter
        return texture

    def get_latest_frame(self):
        with self._lock:
            return self._latest

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._cancel_watchdog()
        self._teardown_pipeline()

    # -- streaming-thread callback -----------------------------------------

    def _on_new_sample(self, sink):
        if _HAVE_GSTAPP:
            sample = sink.pull_sample()
        else:
            sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR

        buf = sample.get_buffer()
        caps = sample.get_caps()
        if caps is not None:
            caps_str = caps.to_string()
            if caps_str != self._caps_str:
                self._caps_str = caps_str
                struct = caps.get_structure(0)
                self._caps_w = struct.get_value("width")
                self._caps_h = struct.get_value("height")
                self._caps_fmt = struct.get_value("format")

        w, h, fmt = self._caps_w, self._caps_h, self._caps_fmt

        stride = None
        if _HAVE_GSTVIDEO:
            meta = GstVideo.buffer_get_video_meta(buf)
            if meta is not None:
                stride = meta.stride[0]
        if stride is None:
            stride = w * 4 if w else 0

        data = buf.extract_dup(0, buf.get_size())

        schedule = False
        with self._lock:
            self._latest = (data, w, h, stride, fmt)
            self._frame_counter += 1
            if not self._pending:
                self._pending = True
                schedule = True

        if schedule:
            GLib.idle_add(self._on_idle_notify)

        return Gst.FlowReturn.OK

    # -- main-loop callbacks ------------------------------------------------

    def _on_idle_notify(self) -> bool:
        with self._lock:
            self._pending = False
        self._cancel_watchdog()
        self._on_frame()
        return GLib.SOURCE_REMOVE

    def _on_bus_error(self, bus, message) -> None:
        err, dbg = message.parse_error()

        if (
            self._pipeline_override is None
            and not self._retried_caps
            and _is_not_negotiated(err, dbg)
        ):
            self._retried_caps = True
            self._teardown_pipeline()
            try:
                self._build_pipeline(restrict_format=False)
            except ValueError:
                self._on_error(f"{err}: {dbg}")
                return
            self.start()
            return

        self._on_error(f"{err}: {dbg}")


# ==== src/ui.py ====
# --------------------------------------------------------------------------
# Constants (binding, see .superpowers/sdd/interfaces.md)
# --------------------------------------------------------------------------

LENS_W, LENS_H = 480, 320
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
    - If none of the four has that much room (only possible in small windows
      at low zoom, since the lens is large relative to the window), we fall
      back to whichever direction has the most room. Non-overlap is enforced
      unconditionally as a final safety clamp: even when the window is too
      small to keep the lens fully inside it, the lens's BORDER-expanded
      outer rect is guaranteed to never intersect the source rect — the lens
      is allowed to hang slightly outside the window edge instead, which is
      harmless (no camera feedback loop) and bounded by a few pixels.
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
        pill_w, pill_h = 64, 28
        px, py = lx + pad, ly + pad

        pill_rounded = Gsk.RoundedRect()
        pill_rounded.init_from_rect(Graphene.Rect().init(px, py, pill_w, pill_h), 8)
        dark = Gdk.RGBA()
        dark.parse("rgba(0,0,0,0.7)")
        snapshot.push_rounded_clip(pill_rounded)
        snapshot.append_color(dark, Graphene.Rect().init(px, py, pill_w, pill_h))
        snapshot.pop()

        layout = self.create_pango_layout(f"{zoom:.2g}x")
        layout.set_alignment(Pango.Alignment.CENTER)
        white = Gdk.RGBA()
        white.parse("white")

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


# ==== src/main.py ====
# NOTE: these are `from X import name` (not `import X`) deliberately — after
# tools/build.py bundles all five src modules into one flat loupe.py file,
# there is no separate `portal`/`framesource`/`ui`/`lifecycle` namespace to
# qualify against; only the bare names it imports here still resolve.

APP_ID = "dev.andrzey.loupe"

MARKER_SIZE = 16
SEARCH_ROWS = 300
SEARCH_COLS = 400
CALIBRATION_MAX_FRAMES = 30
CALIBRATION_TIMEOUT_S = 2.5

_SMOKE_PIPELINE = (
    "videotestsrc is-live=true ! videoconvert ! "
    "video/x-raw,format=BGRx,width=1280,height=720 ! "
    "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
)


# --------------------------------------------------------------------------
# Calibration: pure function, TDD'd against synthetic byte buffers.
# --------------------------------------------------------------------------


def _row_is_marker(data: bytes, stride: int, x: int, y: int, size: int) -> bool:
    row_start = y * stride + x * 4
    for i in range(size):
        off = row_start + i * 4
        if off + 2 >= len(data):
            return False
        if data[off] != 0xFF or data[off + 1] != 0x00 or data[off + 2] != 0xFF:
            return False
    return True


def locate_marker(data: bytes, w: int, h: int, stride: int) -> tuple[int, int] | None:
    """Find the top-left corner of the 16x16 solid magenta (#FF00FF) block
    LoupeWindow draws at window (0,0) while calibrating.

    Searches rows 0..min(h,300), cols 0..min(w,400). A match requires a
    16-pixel horizontal run of magenta AND matching spot-check runs on rows
    +4/+8/+15 (guards against coincidental magenta elsewhere in the frame).
    Returns (x, y) in frame pixel coordinates, or None if not found.
    """
    x_limit = min(w, SEARCH_COLS)
    y_limit = min(h, SEARCH_ROWS)

    for y in range(y_limit):
        if y + (MARKER_SIZE - 1) >= h:
            continue
        for x in range(x_limit):
            if x + MARKER_SIZE > w:
                continue
            if not _row_is_marker(data, stride, x, y, MARKER_SIZE):
                continue
            if not _row_is_marker(data, stride, x, y + 4, MARKER_SIZE):
                continue
            if not _row_is_marker(data, stride, x, y + 8, MARKER_SIZE):
                continue
            if not _row_is_marker(data, stride, x, y + 15, MARKER_SIZE):
                continue
            return (x, y)
    return None


# --------------------------------------------------------------------------
# Application wiring
# --------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    if "--test-portal" in argv:
        try:
            _run_test_portal()
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1
        return 0

    smoke = "--smoke" in argv

    if not smoke:
        if not acquire_pidfile_or_toggle():
            return 0

        if not have_pipewiresrc():
            fail(
                "GStreamer PipeWire plugin missing",
                hint="sudo apt install gstreamer1.0-pipewire gir1.2-gst-plugins-base-1.0",
            )
            release_pidfile()
            return 1

    app = Gtk.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
    state = {"fs": None, "portal": None, "cleaned_up": False}

    def cleanup():
        if state["cleaned_up"]:
            return
        state["cleaned_up"] = True
        if state["fs"] is not None:
            state["fs"].stop()
        if state["portal"] is not None:
            state["portal"].close()
        if not smoke:
            release_pidfile()
        app.quit()

    def make_calibration(window):
        counter = {"frames": 0, "done": False, "timeout_id": None}

        def finish_fallback():
            if counter["done"]:
                return GLib.SOURCE_REMOVE
            counter["done"] = True
            fs = state["fs"]
            frame = fs.get_latest_frame() if fs is not None else None
            frame_h = frame[2] if frame is not None else window.get_height()
            print(
                "loupe: calibration marker not found, falling back to "
                "ox=0, oy=frame_h - window_height",
                file=sys.stderr,
            )
            window.set_frame_offset(0, frame_h - window.get_height())
            return GLib.SOURCE_REMOVE

        counter["timeout_id"] = GLib.timeout_add(
            int(CALIBRATION_TIMEOUT_S * 1000), finish_fallback
        )

        def on_frame():
            if not counter["done"]:
                fs = state["fs"]
                frame = fs.get_latest_frame() if fs is not None else None
                if frame is not None:
                    data, w, h, stride, _fmt = frame
                    found = locate_marker(data, w, h, stride)
                    if found is not None:
                        counter["done"] = True
                        if counter["timeout_id"] is not None:
                            GLib.source_remove(counter["timeout_id"])
                            counter["timeout_id"] = None
                        mx, my = found
                        window.set_frame_offset(mx, my)
                    else:
                        counter["frames"] += 1
                        if counter["frames"] >= CALIBRATION_MAX_FRAMES:
                            if counter["timeout_id"] is not None:
                                GLib.source_remove(counter["timeout_id"])
                                counter["timeout_id"] = None
                            finish_fallback()
            window.notify_frame()

        return on_frame

    def on_activate(app):
        install_signal_handlers(cleanup)

        def start_frame_source(node_id, fd, props):
            def on_fs_error(message):
                fail(message)
                cleanup()

            # window doesn't exist until after FrameSource is constructed, and
            # calibration needs the window — dispatch through a holder that's
            # populated once the window is built.
            frame_cb_holder = {"fn": lambda: None}

            def on_frame():
                frame_cb_holder["fn"]()

            pipeline_override = _SMOKE_PIPELINE if smoke else None
            fs = FrameSource(
                fd, node_id, on_frame, on_fs_error, pipeline_override=pipeline_override
            )
            state["fs"] = fs
            fs.start()

            window = LoupeWindow(application=app, frame_source=fs, on_quit=cleanup)
            frame_cb_holder["fn"] = make_calibration(window)
            window.present()
            app.release()

        on_ready = start_frame_source

        def on_portal_error(code, message):
            if code == 1:
                cleanup()
                return
            fail(message)
            cleanup()

        if smoke:
            app.hold()
            start_frame_source(0, -1, {})
            return

        ps = PortalScreenCast()
        state["portal"] = ps
        app.hold()
        ps.start(on_ready, on_portal_error)

    app.connect("activate", on_activate)
    return app.run(None)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
