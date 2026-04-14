import sys
from pathlib import Path

import pytest
from shapely.geometry import Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.outline import OutlineExtractorV2
from core.parser import Point, Segment


def _segment(start, end):
    return Segment(Point(*start), Point(*end), {"type": "line"})


def _add_axis_aligned_edge(segments, start, end, gap_size=0.0):
    if gap_size <= 0:
        segments.append(_segment(start, end))
        return

    x1, y1 = start
    x2, y2 = end
    if x1 == x2:
        mid = (y1 + y2) / 2.0
        half_gap = gap_size / 2.0
        first_end = (x1, mid - half_gap)
        second_start = (x1, mid + half_gap)
    else:
        mid = (x1 + x2) / 2.0
        half_gap = gap_size / 2.0
        first_end = (mid - half_gap, y1)
        second_start = (mid + half_gap, y1)

    segments.append(_segment(start, first_end))
    segments.append(_segment(second_start, end))


def _add_ring(segments, points, gap_edge=None, gap_size=0.0):
    for index, start in enumerate(points):
        end = points[(index + 1) % len(points)]
        edge_gap = gap_size if gap_edge == index else 0.0
        _add_axis_aligned_edge(segments, start, end, gap_size=edge_gap)


def _add_connectors(segments, outer_points, inner_points):
    for outer, inner in zip(outer_points, inner_points):
        segments.append(_segment(outer, inner))


def _build_double_wall_ring(outer_points, inner_points, gap_edge=None, gap_size=0.0):
    segments = []
    _add_ring(segments, outer_points, gap_edge=gap_edge, gap_size=gap_size)
    _add_ring(segments, inner_points, gap_edge=gap_edge, gap_size=gap_size)
    _add_connectors(segments, outer_points, inner_points)
    return segments


def _build_noded_double_wall_rectangle(outer_rect, thickness, gap_edge=None, gap_size=0.0):
    min_x, min_y, max_x, max_y = outer_rect
    inner_rect = (min_x + thickness, min_y + thickness, max_x - thickness, max_y - thickness)

    outer = [
        (min_x, min_y),
        ((min_x + max_x) / 2.0, min_y),
        (max_x, min_y),
        (max_x, (min_y + max_y) / 2.0),
        (max_x, max_y),
        ((min_x + max_x) / 2.0, max_y),
        (min_x, max_y),
        (min_x, (min_y + max_y) / 2.0),
    ]
    inner = [
        (inner_rect[0], inner_rect[1]),
        ((inner_rect[0] + inner_rect[2]) / 2.0, inner_rect[1]),
        (inner_rect[2], inner_rect[1]),
        (inner_rect[2], (inner_rect[1] + inner_rect[3]) / 2.0),
        (inner_rect[2], inner_rect[3]),
        ((inner_rect[0] + inner_rect[2]) / 2.0, inner_rect[3]),
        (inner_rect[0], inner_rect[3]),
        (inner_rect[0], (inner_rect[1] + inner_rect[3]) / 2.0),
    ]

    segments = []
    _add_ring(segments, outer, gap_edge=gap_edge, gap_size=gap_size)
    _add_ring(segments, inner, gap_edge=gap_edge, gap_size=gap_size)
    for index in (1, 3, 5, 7):
        segments.append(_segment(outer[index], inner[index]))
    return segments


def _build_noded_double_wall_l_shape():
    outer = [
        (0, 0), (210, 0), (420, 0), (420, 85), (420, 170), (295, 170),
        (170, 170), (170, 295), (170, 420), (85, 420), (0, 420), (0, 210),
    ]
    inner = [
        (40, 40), (210, 40), (380, 40), (380, 85), (380, 130), (255, 130),
        (130, 130), (130, 255), (130, 380), (85, 380), (40, 380), (40, 210),
    ]

    segments = []
    _add_ring(segments, outer)
    _add_ring(segments, inner)
    for index in (1, 3, 5, 7, 9, 11):
        segments.append(_segment(outer[index], inner[index]))
    return segments


def test_outline_v2_prefers_open_outer_shell_over_closed_inner_room():
    outer = [(0, 0), (1000, 0), (1000, 600), (0, 600)]
    segments = _build_noded_double_wall_rectangle((0, 0, 1000, 600), 40.0, gap_edge=5, gap_size=30.0)

    room = [(300, 180), (450, 180), (450, 330), (300, 330)]
    _add_ring(segments, room)

    polygon, metadata = OutlineExtractorV2().extract_boundary(segments)

    assert polygon is not None
    assert polygon.area == pytest.approx(Polygon(outer).area, rel=0.08)
    assert len(polygon.interiors) == 0
    assert metadata["estimate_method"] == "orthogonal_connectors"


def test_outline_v2_preserves_courtyard_hole():
    outer = [(0, 0), (1000, 0), (1000, 800), (0, 800)]
    segments = _build_noded_double_wall_rectangle((0, 0, 1000, 800), 40.0)

    courtyard_inner = [(360, 260), (640, 260), (640, 540), (360, 540)]
    segments.extend(_build_noded_double_wall_rectangle((320, 220, 680, 580), 40.0))

    polygon, _ = OutlineExtractorV2().extract_boundary(segments)

    assert polygon is not None
    assert len(polygon.interiors) == 1
    assert Polygon(polygon.interiors[0]).area == pytest.approx(Polygon(courtyard_inner).area, rel=0.15)


def test_outline_v2_handles_l_shaped_double_wall():
    outer = [(0, 0), (420, 0), (420, 170), (170, 170), (170, 420), (0, 420)]
    segments = _build_noded_double_wall_l_shape()

    polygon, _ = OutlineExtractorV2().extract_boundary(segments)

    assert polygon is not None
    assert polygon.area == pytest.approx(Polygon(outer).area, rel=0.18)


def test_outline_v2_falls_back_to_parallel_pairs_without_connectors():
    segments = []
    _add_ring(segments, [(0, 0), (1000, 0), (1000, 600), (0, 600)], gap_edge=2, gap_size=20.0)
    _add_ring(segments, [(40, 40), (960, 40), (960, 560), (40, 560)], gap_edge=2, gap_size=20.0)

    polygon, metadata = OutlineExtractorV2().extract_boundary(segments)

    assert polygon is not None
    assert polygon.area > 500000
    assert metadata["estimate_method"] in {"parallel_pairs", "bbox_fallback"}
