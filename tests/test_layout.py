"""Tests for compute_layout: the lens (80% of window width x LENS_H) is
centered on the cursor but clamped to stay fully inside the viewport. The
magnified pixel under the cursor stays under the cursor everywhere — when the
lens hits an edge it stops moving and the source region shifts instead.
"""
import pytest

from src.ui import (
    LENS_H,
    LENS_WIDTH_FRAC,
    ZOOM_DEFAULT,
    ZOOM_MAX,
    ZOOM_MIN,
    Layout,
    compute_layout,
)

WIN_W, WIN_H = 1920, 1132  # the maximized work area on the target machine
LENS_W = LENS_WIDTH_FRAC * WIN_W  # 1536 on the target machine

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


def test_lens_width_tracks_window_width():
    for win_w in (1280, 1920, 3440):
        layout = compute_layout(win_w / 2, 400, ZOOM_DEFAULT, win_w, WIN_H)
        _lx, _ly, lw, _lh = layout.lens
        assert lw == pytest.approx(LENS_WIDTH_FRAC * win_w)


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


def test_undersized_window_pins_lens_vertically():
    # Window shorter than the lens (e.g. dev harness): degrade gracefully by
    # pinning to the top rather than producing negative clamp bounds.
    layout = compute_layout(300, 200, ZOOM_DEFAULT, 640, 400)
    _lx, ly, _, _ = layout.lens
    assert ly == 0.0


def test_spot_check_default_zoom():
    # cursor at (960, 566), zoom 2.5, window 1920x1132:
    # lens = 1536x576, centered fits: lx = clamp(960-768, 0, 384) = 192,
    #                                 ly = clamp(566-288, 0, 556) = 278
    # src: sw = 1536/2.5 = 614.4, sh = 576/2.5 = 230.4
    #      sx = 960 - (960-192)/2.5 = 652.8, sy = 566 - (566-278)/2.5 = 450.8
    layout = compute_layout(960, 566, 2.5, WIN_W, WIN_H)
    assert layout.src == pytest.approx((652.8, 450.8, 614.4, 230.4))
    assert layout.lens == pytest.approx((192.0, 278.0, 1536.0, 576.0))


def test_constants():
    assert LENS_WIDTH_FRAC == 0.8
    assert LENS_H == 576
    assert ZOOM_MIN == 1.5
    assert ZOOM_MAX == 8.0
    assert ZOOM_DEFAULT == 2.5
