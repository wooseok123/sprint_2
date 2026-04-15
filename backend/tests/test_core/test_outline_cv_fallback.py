import sys
from pathlib import Path
import math

import pytest
from shapely.geometry import Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.outline_cv_fallback import (
    _collect_roi_segments,
    _detect_door_pattern,
    _render_roi_to_image,
    apply_cv_door_fallback,
    collect_cv_candidate_bounds,
    cv2,
)
from core.parser import Point, Segment


pytestmark = pytest.mark.skipif(cv2 is None, reason="cv2 is not installed")


def _line(start, end):
    return Segment(Point(*start), Point(*end), {"type": "line"})


def _arc_segments(center, radius, start_deg, end_deg, group_id=1, steps=12):
    start_rad = math.radians(start_deg)
    end_rad = math.radians(end_deg)
    step = (end_rad - start_rad) / steps
    points = []
    for index in range(steps + 1):
        angle = start_rad + step * index
        points.append((
            center[0] + radius * math.cos(angle),
            center[1] + radius * math.sin(angle),
        ))

    segments = []
    for start, end in zip(points, points[1:]):
        segments.append(Segment(
            Point(*start),
            Point(*end),
            {
                "type": "arc_segment",
                "center": center,
                "radius": radius,
                "sweep_angle_deg": abs(end_deg - start_deg),
                "arc_group_id": group_id,
                "arc_start": list(points[0]),
                "arc_end": list(points[-1]),
                "source_entity": "TEST_ARC",
            },
        ))
    return segments


def test_quarter_arc_door_with_frame():
    polygon = Polygon([
        (0, 0),
        (1000, 0),
        (1000, 220),
        (1040, 220),
        (1080, 240),
        (1100, 280),
        (1100, 320),
        (1000, 320),
        (1000, 600),
        (0, 600),
    ])

    segments = [
        _line((0, 0), (1000, 0)),
        _line((1000, 320), (1000, 600)),
        _line((0, 600), (1000, 600)),
        _line((0, 0), (0, 600)),
        _line((1000, 220), (1040, 220)),
        _line((1040, 220), (1040, 260)),
        _line((1000, 320), (1040, 320)),
        _line((1040, 280), (1040, 320)),
    ]
    segments.extend(_arc_segments((1000, 320), 100, -90, 0, group_id=7))

    result, metadata = apply_cv_door_fallback(
        polygon=polygon,
        parsed_segments=segments,
        candidate_bounds=(995, 215, 1105, 325),
        wall_thickness=200,
    )

    assert result is not None
    assert metadata["applied"] is True
    assert metadata["frame_segment_count"] >= 2
    assert metadata["detection_score"] > 0.5
    assert result.area < polygon.area - 10000
    assert result.area > polygon.area * 0.70
    assert result.bounds[2] < polygon.bounds[2]


def test_collect_cv_candidate_bounds_finds_residual_arc_roi():
    polygon = Polygon([
        (0, 0),
        (1000, 0),
        (1000, 220),
        (1040, 220),
        (1080, 240),
        (1100, 280),
        (1100, 320),
        (1000, 320),
        (1000, 600),
        (0, 600),
    ])

    segments = [
        _line((0, 0), (1000, 0)),
        _line((1000, 320), (1000, 600)),
        _line((0, 600), (1000, 600)),
        _line((0, 0), (0, 600)),
        _line((1000, 220), (1040, 220)),
        _line((1040, 220), (1040, 260)),
        _line((1000, 320), (1040, 320)),
        _line((1040, 280), (1040, 320)),
    ]
    segments.extend(_arc_segments((1000, 320), 100, -90, 0, group_id=9))

    candidate_bounds = collect_cv_candidate_bounds(
        polygon=polygon,
        parsed_segments=segments,
        wall_thickness=200,
    )

    assert candidate_bounds
    assert candidate_bounds[0][2] >= 1090


def test_diagonal_leaf_door_pattern_is_detected():
    polygon = Polygon([
        (0, 0),
        (1000, 0),
        (1000, 220),
        (1120, 220),
        (1120, 260),
        (1000, 260),
        (1000, 600),
        (0, 600),
    ])

    segments = [
        _line((0, 0), (1000, 0)),
        _line((1000, 260), (1000, 600)),
        _line((0, 600), (1000, 600)),
        _line((0, 0), (0, 600)),
        _line((1000, 220), (1120, 220)),
        _line((1000, 260), (1120, 260)),
        _line((1000, 220), (1000, 260)),
        _line((1120, 220), (1120, 235)),
        _line((1120, 245), (1120, 260)),
        _line((1120, 260), (1000, 380)),
    ]

    candidate_bounds = (995, 215, 1125, 265)
    roi_segments, roi_bounds = _collect_roi_segments(
        segments,
        candidate_bounds,
        padding=40.0,
    )
    image, render_meta = _render_roi_to_image(
        roi_segments,
        roi_bounds,
        padding=0.0,
        resolution=4.0,
    )
    detection = _detect_door_pattern(
        image=image,
        segments=roi_segments,
        candidate_bounds=candidate_bounds,
        wall_thickness=25,
        roi_bounds=roi_bounds,
        resolution=render_meta["resolution_mm_per_px"],
    )

    assert detection is not None
    assert detection.pattern_type == "diagonal_frame"
    assert detection.frame_segment_count >= 2
    assert detection.score > 0.8


def test_cv_fallback_returns_none_on_curved_bay_without_frame():
    polygon = Polygon([
        (0, 0),
        (1000, 0),
        (1000, 220),
        (1040, 220),
        (1080, 240),
        (1100, 280),
        (1100, 320),
        (1000, 320),
        (1000, 600),
        (0, 600),
    ])

    segments = [
        _line((0, 0), (1000, 0)),
        _line((1000, 320), (1000, 600)),
        _line((0, 600), (1000, 600)),
        _line((0, 0), (0, 600)),
    ]
    segments.extend(_arc_segments((1000, 320), 100, -90, 0, group_id=8))

    result, metadata = apply_cv_door_fallback(
        polygon=polygon,
        parsed_segments=segments,
        candidate_bounds=(995, 215, 1105, 325),
        wall_thickness=200,
    )

    assert result is None
    assert metadata["applied"] is False
    assert metadata["detection_reason"] in {"no_door_pattern", "guard_rejected"}
