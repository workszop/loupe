"""Tests for compute_layout: the lens is centered on the cursor but clamped to
stay fully inside the viewport. The magnified pixel under the cursor stays
under the cursor everywhere — when the lens hits an edge it stops moving and
the source region shifts instead.
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

WIN_W, WIN_H = 1920, 1132  # the maximized work area on the target machine

ZOOMS = [ZOOM_MIN, 2.0, ZOOM_DEFAULT, 4.0, ZOOM_MAX]
POSITIONS = [
    (960, 566),      # center
    (0, 0),          # top-left corner
    (1920, 1132),    # bottom-right corner
    (100, 1100),     # near bottom-left
    (1850, 40),      # near top-right
]


@pytest.mark.parametrize("zoom", ZOOMS)
@pytest.mark.parametrize("cx,cy", POSITIONS)
def test_lens_stays_inside_viewport(cx, cy, zoom):
    layout = compute_layout(cx, cy, zoom, WIN_W, WIN_H)
    assert isinstance(layout, Layout)
    lx, ly, lw, lh = layout.lens
    assert (lw, lh) == (LENS_W, LENS_H)
    assert 0 <= lx <= WIN_W - LENS_W
    assert 0 <= ly <= WIN_H - LENS_H


@pytest.mark.parametrize("zoom", ZOOMS)
def test_lens_centered_when_away_from_edges(zoom):
    cx, cy = 960, 566
    layout = compute_layout(cx, cy, zoom, WIN_W, WIN_H)
    lx, ly, lw, lh = layout.lens
    assert lx + lw / 2 == pytest.approx(cx)
    assert ly + lh / 2 == pytest.approx(cy)


@pytest.mark.parametrize("zoom", ZOOMS)
@pytest.mark.parametrize("cx,cy", POSITIONS)
def test_cursor_pixel_stays_under_cursor(cx, cy, zoom):
    # The invariant that makes aiming (and click-through) intuitive: the
    # magnified image of the point under the cursor renders exactly at the
    # cursor, clamped or not. src maps onto lens with scale == zoom, so the
    # dest position of source point cx is lx + (cx - sx) * zoom.
    layout = compute_layout(cx, cy, zoom, WIN_W, WIN_H)
    sx, sy, _sw, _sh = layout.src
    lx, ly, _lw, _lh = layout.lens
    assert lx + (cx - sx) * zoom == pytest.approx(cx)
    assert ly + (cy - sy) * zoom == pytest.approx(cy)


@pytest.mark.parametrize("zoom", ZOOMS)
@pytest.mark.parametrize("cx,cy", POSITIONS)
def test_source_size_scales_with_zoom(cx, cy, zoom):
    layout = compute_layout(cx, cy, zoom, WIN_W, WIN_H)
    _sx, _sy, sw, sh = layout.src
    assert sw == pytest.approx(LENS_W / zoom)
    assert sh == pytest.approx(LENS_H / zoom)


def test_clamped_at_top_left_corner():
    # Cursor in the very corner: lens pinned to the corner, and the source
    # region starts at the cursor (nothing above/left of it is shown).
    layout = compute_layout(0, 0, ZOOM_DEFAULT, WIN_W, WIN_H)
    lx, ly, _, _ = layout.lens
    sx, sy, _, _ = layout.src
    assert (lx, ly) == (0.0, 0.0)
    assert (sx, sy) == (0.0, 0.0)


def test_undersized_window_pins_lens_to_origin():
    # Window smaller than the lens (e.g. dev harness): degrade gracefully by
    # pinning to the top-left rather than producing negative clamp bounds.
    layout = compute_layout(300, 200, ZOOM_DEFAULT, 640, 400)
    lx, ly, _, _ = layout.lens
    assert (lx, ly) == (0.0, 0.0)


def test_spot_check_default_zoom_centered():
    # cursor at (960, 566), zoom 2.5 — far from edges, so identical to the
    # old unclamped placement:
    # sw = 1440/2.5 = 576, sh = 480/2.5 = 192
    # src centered: (960-288, 566-96) = (672, 470)
    # lens centered: (960-720, 566-240) = (240, 326)
    layout = compute_layout(960, 566, 2.5, WIN_W, WIN_H)
    assert layout.src == pytest.approx((672.0, 470.0, 576.0, 192.0))
    assert layout.lens == pytest.approx((240.0, 326.0, 1440.0, 480.0))


def test_constants():
    assert LENS_W == 1440
    assert LENS_H == 480
    assert ZOOM_MIN == 1.5
    assert ZOOM_MAX == 8.0
    assert ZOOM_DEFAULT == 2.5
