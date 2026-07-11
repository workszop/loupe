"""Tests for src/framesource.py.

These run real GStreamer pipelines (videotestsrc, no display needed).
pipewiresrc is not installed on this machine, so all live pipelines use
pipeline_override with videotestsrc, per the task brief.
"""
import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gst  # noqa: E402

import pytest  # noqa: E402

from framesource import FrameSource, have_pipewiresrc  # noqa: E402

Gst.init(None)

VIDEOTESTSRC_OVERRIDE = (
    "videotestsrc is-live=true ! videoconvert ! "
    "video/x-raw,format=BGRx,width=320,height=240 ! "
    "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
)


def run_loop_until(loop, predicate, timeout_s):
    """Pump the given GLib.MainLoop until predicate() is true or timeout."""
    deadline = GLib.get_monotonic_time() + int(timeout_s * 1_000_000)

    def check():
        if predicate() or GLib.get_monotonic_time() >= deadline:
            loop.quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    GLib.timeout_add(10, check)
    loop.run()


@pytest.fixture
def loop():
    return GLib.MainLoop()


@pytest.fixture
def frame_events():
    return {"count": 0, "errors": []}


@pytest.fixture
def source(frame_events):
    fs = FrameSource(
        fd=-1,
        node_id=0,
        on_frame=lambda: frame_events.__setitem__("count", frame_events["count"] + 1),
        on_error=lambda msg: frame_events["errors"].append(msg),
        pipeline_override=VIDEOTESTSRC_OVERRIDE,
    )
    fs.start()
    yield fs
    fs.stop()


def test_have_pipewiresrc_returns_bool():
    assert isinstance(have_pipewiresrc(), bool)


def test_invalid_override_without_named_sink_raises_valueerror(frame_events):
    with pytest.raises(ValueError):
        FrameSource(
            fd=-1,
            node_id=0,
            on_frame=lambda: None,
            on_error=lambda msg: None,
            pipeline_override="fakesrc ! fakesink",
        )


def test_on_frame_fires_and_latest_frame_matches_caps(source, loop, frame_events):
    run_loop_until(loop, lambda: frame_events["count"] >= 1, timeout_s=2)
    assert frame_events["count"] >= 1, "on_frame did not fire within 2s"
    assert frame_events["errors"] == []

    frame = source.get_latest_frame()
    assert frame is not None
    data, w, h, stride, fmt = frame
    assert w == 320
    assert h == 240
    assert stride >= w * 4
    assert fmt == "BGRx"
    assert len(data) >= stride * h


def test_get_texture_matches_size_and_caches(source, loop, frame_events):
    run_loop_until(loop, lambda: frame_events["count"] >= 1, timeout_s=2)
    assert frame_events["count"] >= 1

    tex1 = source.get_texture()
    assert isinstance(tex1, Gdk.MemoryTexture)
    assert tex1.get_width() == 320
    assert tex1.get_height() == 240

    tex2 = source.get_texture()
    assert tex2 is tex1, "get_texture() must return the cached object without a new frame"


def test_coalescing_never_more_than_one_pending_idle(source, loop, frame_events):
    import time

    # Let several samples land on the GStreamer streaming thread *without*
    # ever running the GLib main loop, so any idle_add() calls just queue up
    # without being processed. If coalescing works, only one idle can be
    # pending no matter how many frames arrived in the meantime.
    time.sleep(0.4)
    assert source._frame_counter > 1, "expected multiple frames to have landed"
    assert frame_events["count"] == 0, "on_frame must not fire without pumping the loop"
    assert source._pending is True

    # Freeze the streaming thread so no further samples land while we drain
    # the single queued idle -- otherwise a fresh frame racing in during
    # loop.run() would legitimately re-arm _pending, which is correct
    # coalescing behaviour but would make this assertion flaky.
    source._pipeline.set_state(Gst.State.PAUSED)
    frames_before_drain = source._frame_counter

    # Draining a single idle iteration must fire on_frame exactly once, even
    # though many more frames than that arrived while the loop wasn't
    # running.
    GLib.idle_add(lambda: (loop.quit(), GLib.SOURCE_REMOVE)[1])
    loop.run()

    assert frame_events["count"] == 1
    assert source._pending is False
    assert source._frame_counter == frames_before_drain

    source._pipeline.set_state(Gst.State.PLAYING)


def test_stop_is_idempotent_and_get_texture_safe_after_stop(source, loop, frame_events):
    run_loop_until(loop, lambda: frame_events["count"] >= 1, timeout_s=2)
    assert frame_events["count"] >= 1

    tex_before = source.get_texture()

    source.stop()
    source.stop()  # must not raise

    tex_after = source.get_texture()
    assert tex_after is tex_before or tex_after is None


def test_watchdog_fires_when_no_frames_arrive(loop, frame_events):
    # fakesrc num-buffers=0 reaches PLAYING and posts EOS without ever
    # delivering a sample to appsink -- a deterministic way to exercise the
    # "no frames" watchdog path without needing pipewiresrc.
    fs = FrameSource(
        fd=-1,
        node_id=0,
        on_frame=lambda: frame_events.__setitem__("count", frame_events["count"] + 1),
        on_error=lambda msg: frame_events["errors"].append(msg),
        pipeline_override=(
            "fakesrc num-buffers=0 ! video/x-raw,width=320,height=240,format=BGRx "
            "! appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
        ),
        watchdog_sec=1,
    )
    fs.start()
    try:
        run_loop_until(loop, lambda: len(frame_events["errors"]) >= 1, timeout_s=3)
        assert frame_events["count"] == 0
        assert len(frame_events["errors"]) == 1
        assert "no frames after 1s" in frame_events["errors"][0]
    finally:
        fs.stop()


def test_watchdog_cancelled_once_first_frame_arrives(source, loop, frame_events):
    # source fixture uses default watchdog_sec=5; if a frame arrives promptly
    # the watchdog must not later fire on_error.
    run_loop_until(loop, lambda: frame_events["count"] >= 1, timeout_s=2)
    assert frame_events["count"] >= 1
    assert source._watchdog_id is None


def test_is_not_negotiated_detection():
    from framesource import _is_not_negotiated

    err = GLib.Error.new_literal(
        Gst.stream_error_quark(),
        "Internal data stream error.",
        int(Gst.StreamError.FORMAT),
    )
    assert _is_not_negotiated(err, "streaming stopped, reason not-negotiated (-4)")

    other_err = GLib.Error.new_literal(
        Gst.stream_error_quark(),
        "Internal data stream error.",
        int(Gst.StreamError.FORMAT),
    )
    assert not _is_not_negotiated(other_err, "some other failure")
