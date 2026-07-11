"""Property + spot-check tests for compute_layout (pure math, no GTK runtime deps
beyond what src/ui.py itself imports at module load time).

TDD: this file is written BEFORE src/ui.py exists / before compute_layout is
implemented. Run once to confirm RED (ImportError), then implement, then GREEN.
"""
import itertools

import pytest

from src.ui import (
    BORDER,
    LENS_H,
    LENS_W,
    MARGIN,
    ZOOM_MAX,
    ZOOM_MIN,
    Layout,
    compute_layout,
)

ZOOMS = [1.5, 2.0, 2.5, 4.0, 8.0]

EPS = 1e-6


def rects_intersect(a, b):
    """Strict intersection test: touching edges (equal coordinates) do NOT count
    as intersecting."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by


def rect_in_window(r, win_w, win_h, tol=EPS):
    x, y, w, h = r
    return (
        x >= -tol
        and y >= -tol
        and x + w <= win_w + tol
        and y + h <= win_h + tol
    )


def rect_contains_point(r, px, py, tol=EPS):
    x, y, w, h = r
    return x - tol <= px <= x + w + tol and y - tol <= py <= y + h + tol


def outer_rect(lens):
    x, y, w, h = lens
    return (x - BORDER, y - BORDER, w + 2 * BORDER, h + 2 * BORDER)


def grid(win_w, win_h, cols=17, rows=11):
    """17x11 grid of cursor positions spanning the window, including exact
    corners and edges."""
    for j in range(rows):
        cy = win_h * j / (rows - 1)
        for i in range(cols):
            cx = win_w * i / (cols - 1)
            yield cx, cy


# ---------------------------------------------------------------------------
# Primary property sweep: 1920x1132, full containment expected exactly.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("zoom", ZOOMS)
def test_primary_sweep_1920x1132(zoom):
    win_w, win_h = 1920, 1132
    for cx, cy in grid(win_w, win_h):
        layout = compute_layout(cx, cy, zoom, win_w, win_h)
        assert isinstance(layout, Layout)
        src = layout.src
        lens = layout.lens

        assert not rects_intersect(outer_rect(lens), src), (
            f"lens outer overlaps src at cx={cx},cy={cy},zoom={zoom}: "
            f"src={src} lens={lens}"
        )
        assert rect_in_window(lens, win_w, win_h), (
            f"lens not fully in window at cx={cx},cy={cy},zoom={zoom}: lens={lens}"
        )
        assert rect_in_window(src, win_w, win_h), (
            f"src not fully in window at cx={cx},cy={cy},zoom={zoom}: src={src}"
        )
        assert rect_contains_point(src, cx, cy), (
            f"src does not contain cursor at cx={cx},cy={cy},zoom={zoom}: src={src}"
        )
        assert lens[2] == LENS_W and lens[3] == LENS_H


# ---------------------------------------------------------------------------
# Window-size sweep: non-overlap + src properties are unconditional (the hard
# guarantee). Full lens containment is asserted exactly for windows with ample
# room (2560x1440); at the smallest supported size (1280x720) the geometry is
# occasionally so tight (zoom=1.5, cursor near dead-center) that avoiding
# overlap requires the lens to poke very slightly outside the window edge —
# bounded by BORDER px, since non-overlap always wins over containment. We
# assert containment with a BORDER-sized tolerance there.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("win_w,win_h", [(1280, 720), (2560, 1440)])
@pytest.mark.parametrize("zoom", ZOOMS)
def test_window_size_sweep(zoom, win_w, win_h):
    tol = BORDER if (win_w, win_h) == (1280, 720) else EPS
    for cx, cy in grid(win_w, win_h):
        layout = compute_layout(cx, cy, zoom, win_w, win_h)
        src = layout.src
        lens = layout.lens

        assert not rects_intersect(outer_rect(lens), src), (
            f"lens outer overlaps src at cx={cx},cy={cy},zoom={zoom},"
            f"win={win_w}x{win_h}: src={src} lens={lens}"
        )
        assert rect_in_window(src, win_w, win_h), (
            f"src not fully in window at cx={cx},cy={cy},zoom={zoom},"
            f"win={win_w}x{win_h}: src={src}"
        )
        assert rect_contains_point(src, cx, cy), (
            f"src does not contain cursor at cx={cx},cy={cy},zoom={zoom},"
            f"win={win_w}x{win_h}: src={src}"
        )
        assert rect_in_window(lens, win_w, win_h, tol=tol), (
            f"lens not (approx) in window at cx={cx},cy={cy},zoom={zoom},"
            f"win={win_w}x{win_h}: lens={lens}"
        )


# ---------------------------------------------------------------------------
# Exact-value spot checks (hand-computed).
# ---------------------------------------------------------------------------


def test_spot_check_right_placement():
    # cursor near center of a 1920x1132 window, zoom 2.5.
    # sw = 480/2.5 = 192, sh = 320/2.5 = 128
    # sx = clamp(960-96, 0, 1920-192) = 864
    # sy = clamp(566-64, 0, 1132-128) = 502
    # right room = 1920 - (864+192) = 864 >= MARGIN+LENS_W(500) -> right placement
    # lx = 864+192+20 = 1076 ; ly = clamp(566-160, 0, 812) = 406
    layout = compute_layout(960, 566, 2.5, 1920, 1132)
    assert layout.src == pytest.approx((864.0, 502.0, 192.0, 128.0))
    assert layout.lens == pytest.approx((1076.0, 406.0, 480.0, 320.0))


def test_spot_check_left_placement_near_right_edge():
    # cursor near the right edge forces the source to clamp and the right
    # side to overflow, so the lens must fall back to the left.
    # sw=192, sh=128
    # sx = clamp(1900-96, 0, 1728) = 1728 ; sy = clamp(566-64,0,1004) = 502
    # right room = 1920 - (1728+192) = 0 -> doesn't fit -> left room = 1728 -> fits
    # lx = 1728-20-480 = 1228 ; ly = clamp(566-160,0,812) = 406
    layout = compute_layout(1900, 566, 2.5, 1920, 1132)
    assert layout.src == pytest.approx((1728.0, 502.0, 192.0, 128.0))
    assert layout.lens == pytest.approx((1228.0, 406.0, 480.0, 320.0))


# ---------------------------------------------------------------------------
# Constants sanity (binding values from interfaces.md).
# ---------------------------------------------------------------------------


def test_constants():
    assert LENS_W == 480
    assert LENS_H == 320
    assert MARGIN == 20
    assert BORDER == 2.0
    assert ZOOM_MIN == 1.5
    assert ZOOM_MAX == 8.0
