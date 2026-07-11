"""Tests for compute_layout: the freeze-frame magnifier places the lens
centered ON the cursor, with no clamping (it may extend past the screen edge).
"""
import pytest

from src.ui import (
    LENS_H,
    LENS_W,
    ZOOM_DEFAULT,
    ZOOM_MAX,
    ZOOM_MIN,
    Layout,
    compute_layout,
)

ZOOMS = [ZOOM_MIN, 2.0, ZOOM_DEFAULT, 4.0, ZOOM_MAX]


@pytest.mark.parametrize("zoom", ZOOMS)
@pytest.mark.parametrize("cx,cy", [(960, 566), (0, 0), (1920, 1200), (100, 1100), (1850, 40)])
def test_lens_is_centered_on_cursor(cx, cy, zoom):
    layout = compute_layout(cx, cy, zoom)
    assert isinstance(layout, Layout)
    lx, ly, lw, lh = layout.lens
    assert (lw, lh) == (LENS_W, LENS_H)
    # lens center == cursor, exactly, at every position and zoom.
    assert lx + lw / 2 == pytest.approx(cx)
    assert ly + lh / 2 == pytest.approx(cy)


@pytest.mark.parametrize("zoom", ZOOMS)
@pytest.mark.parametrize("cx,cy", [(960, 566), (0, 0), (1920, 1200)])
def test_source_is_centered_on_cursor_and_scales_with_zoom(cx, cy, zoom):
    layout = compute_layout(cx, cy, zoom)
    sx, sy, sw, sh = layout.src
    # The magnified region shrinks as zoom grows, and stays centered on cursor.
    assert sw == pytest.approx(LENS_W / zoom)
    assert sh == pytest.approx(LENS_H / zoom)
    assert sx + sw / 2 == pytest.approx(cx)
    assert sy + sh / 2 == pytest.approx(cy)


def test_no_clamping_lens_may_go_offscreen():
    # Cursor at the top-left corner: the lens is centered there, so its left
    # and top edges are negative (off-screen) — the magnifier must NOT jump it
    # back on-screen.
    layout = compute_layout(0, 0, ZOOM_DEFAULT)
    lx, ly, _, _ = layout.lens
    assert lx == pytest.approx(-LENS_W / 2)
    assert ly == pytest.approx(-LENS_H / 2)


def test_spot_check_default_zoom():
    # cursor at (960, 566), zoom 2.5:
    # sw = 1440/2.5 = 576, sh = 480/2.5 = 192
    # src centered: (960-288, 566-96) = (672, 470)
    # lens centered: (960-720, 566-240) = (240, 326)
    layout = compute_layout(960, 566, 2.5)
    assert layout.src == pytest.approx((672.0, 470.0, 576.0, 192.0))
    assert layout.lens == pytest.approx((240.0, 326.0, 1440.0, 480.0))


def test_constants():
    assert LENS_W == 1440
    assert LENS_H == 480
    assert ZOOM_MIN == 1.5
    assert ZOOM_MAX == 8.0
    assert ZOOM_DEFAULT == 2.5
