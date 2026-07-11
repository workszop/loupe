"""FrameSource: GStreamer (pipewiresrc) -> Gdk.MemoryTexture frames.

No Gtk imports here — only Gst/GLib/Gdk, per the module interface contract.
"""
from __future__ import annotations

import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gst  # noqa: E402

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
