"""Tests for locate_marker in src/main.py.

Pure-bytes tests: build synthetic BGRx-ish frame buffers and check the
calibration marker scan. No GTK/Gst main loop required.
"""
from src.main import locate_marker


def make_frame(w, h, stride=None, fill=(0, 0, 0, 0)):
    if stride is None:
        stride = w * 4
    data = bytearray(stride * h)
    for y in range(h):
        row = y * stride
        for x in range(w):
            off = row + x * 4
            data[off:off + 4] = bytes(fill)
    return data


def paint_marker(data, stride, x, y, size=16, channels=(0xFF, 0x00, 0xFF, 0xFF)):
    for row in range(size):
        for col in range(size):
            off = (y + row) * stride + (x + col) * 4
            data[off:off + 4] = bytes(channels)


def test_marker_at_exact_hit_0_68():
    w, h = 1920, 1200
    data = make_frame(w, h)
    paint_marker(data, w * 4, 0, 68)

    result = locate_marker(bytes(data), w, h, w * 4)

    assert result == (0, 68)


def test_marker_at_arbitrary_position_in_region():
    w, h = 1920, 1200
    data = make_frame(w, h)
    paint_marker(data, w * 4, 137, 211)

    result = locate_marker(bytes(data), w, h, w * 4)

    assert result == (137, 211)


def test_no_marker_returns_none():
    w, h = 1920, 1200
    data = make_frame(w, h)

    result = locate_marker(bytes(data), w, h, w * 4)

    assert result is None


def test_stride_padded_rows_handled():
    w, h = 1920, 1200
    stride = w * 4 + 256  # padded stride, larger than w*4
    data = make_frame(w, h, stride=stride)
    paint_marker(data, stride, 40, 90)

    result = locate_marker(bytes(data), w, h, stride)

    assert result == (40, 90)


def test_near_magenta_15px_run_rejected():
    w, h = 1920, 1200
    data = make_frame(w, h)
    paint_marker(data, w * 4, 10, 10, size=15)  # one pixel short

    result = locate_marker(bytes(data), w, h, w * 4)

    assert result is None


def test_16px_run_with_wrong_row_plus8_rejected():
    w, h = 1920, 1200
    data = make_frame(w, h)
    paint_marker(data, w * 4, 20, 20)
    # corrupt row +8 spot-check within the marker block
    stride = w * 4
    off = (20 + 8) * stride + 20 * 4
    data[off:off + 4] = bytes((0x00, 0x00, 0x00, 0xFF))

    result = locate_marker(bytes(data), w, h, w * 4)

    assert result is None


def test_marker_partially_outside_search_region_returns_none():
    # Search region is rows 0..min(h,300), cols 0..min(w,400).
    # Put the marker's top-left corner just past the column limit.
    w, h = 1920, 1200
    data = make_frame(w, h)
    paint_marker(data, w * 4, 401, 50)

    result = locate_marker(bytes(data), w, h, w * 4)

    assert result is None


def test_marker_top_left_past_row_limit_returns_none():
    w, h = 1920, 1200
    data = make_frame(w, h)
    paint_marker(data, w * 4, 50, 301)

    result = locate_marker(bytes(data), w, h, w * 4)

    assert result is None
