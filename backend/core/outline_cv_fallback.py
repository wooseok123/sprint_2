"""
OpenCV-assisted fallback for door-like curved protrusions.

This module keeps the final edit in vector space. OpenCV is only used to score
whether a suspicious ROI looks like a door swing + frame pattern.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Polygon, box
from shapely.ops import unary_union

from core.parser import Segment

try:  # pragma: no cover - depends on optional runtime dependency
    import cv2
except ImportError:  # pragma: no cover - handled at runtime
    cv2 = None


Bounds = Tuple[float, float, float, float]


@dataclass
class DoorDetectionResult:
    """Detection payload used to build the vector-space removal mask."""

    mask_polygon: Polygon
    arc_group_id: Optional[int]
    frame_segment_count: int
    score: float
    arc_bounds: Bounds
    pattern_type: str = "arc_frame"


@dataclass
class CleanupCandidate:
    """Candidate vector-space cleanup result for a detected door ROI."""

    polygon: Polygon
    method: str
    area_delta: float
    guard_reasons: List[str]
    bbox_ratio: float
    area_ratio: float
    hole_delta: int
    hull_delta: float
    cleanup_radius: float = 0.0


def collect_cv_candidate_bounds(
    polygon: Polygon,
    parsed_segments: List[Segment],
    wall_thickness: float,
    preferred_bounds: Optional[Bounds] = None,
    max_candidates: int = 6,
) -> List[Bounds]:
    """
    Collect suspicious ROI bounds for OpenCV inspection.

    The preferred bridge candidate bounds are tried first, followed by residual
    arc groups still sitting near the final polygon boundary.
    """
    candidates: List[Bounds] = []
    if preferred_bounds is not None:
        candidates.append(tuple(preferred_bounds))

    if polygon is None or polygon.is_empty or wall_thickness <= 0:
        return candidates

    boundary = polygon.boundary
    scored_candidates: List[Tuple[float, Bounds]] = []
    for _, arc_segments in _group_arc_segments(parsed_segments).items():
        sweep_angle = _median_meta_value(arc_segments, "sweep_angle_deg")
        radius = _median_meta_value(arc_segments, "radius")
        if sweep_angle is None or radius is None:
            continue
        if sweep_angle < 40.0 or sweep_angle > 135.0:
            continue
        if radius < wall_thickness * 0.5 or radius > wall_thickness * 12.0:
            continue

        bounds = _segments_bounds(arc_segments)
        expanded = _expand_bounds(bounds, max(wall_thickness * 0.6, 20.0))
        frame_segments = _find_frame_segments(parsed_segments, arc_segments, wall_thickness)
        if len(frame_segments) < 2:
            continue

        distance = boundary.distance(box(*expanded))
        if distance > max(wall_thickness * 8.0, 220.0):
            continue

        score = (
            min(len(frame_segments), 4) * 2.0
            + max(0.0, 1.5 - distance / max(wall_thickness, 1.0))
        )
        scored_candidates.append((score, bounds))

    scored_candidates.sort(key=lambda item: item[0], reverse=True)
    for _, bounds in scored_candidates:
        if len(candidates) >= max_candidates:
            break
        if _bounds_list_contains_overlap(candidates, bounds, overlap_ratio=0.6):
            continue
        candidates.append(bounds)

    return candidates


def apply_cv_door_fallback(
    polygon: Polygon,
    parsed_segments: List[Segment],
    candidate_bounds: Bounds,
    wall_thickness: float,
) -> Tuple[Optional[Polygon], Dict]:
    """
    Detect a door-like ROI with OpenCV and remove it using vector geometry.

    Returns:
        Tuple of (cleaned_polygon_or_none, metadata)
    """
    metadata = {
        "attempted": False,
        "applied": False,
        "cv2_available": cv2 is not None,
        "candidate_bounds": list(candidate_bounds) if candidate_bounds else None,
        "roi_bounds": None,
        "resolution_mm_per_px": 0.0,
        "roi_segment_count": 0,
        "arc_segment_count": 0,
        "frame_segment_count": 0,
        "detection_score": 0.0,
        "pattern_type": None,
        "detection_reason": None,
        "guard_reasons": [],
        "cleanup_method": None,
        "cleanup_radius": 0.0,
        "area_delta": 0.0,
    }

    if cv2 is None:
        metadata["detection_reason"] = "cv2_unavailable"
        return None, metadata
    if polygon is None or polygon.is_empty or not candidate_bounds or wall_thickness <= 0:
        metadata["detection_reason"] = "invalid_input"
        return None, metadata

    metadata["attempted"] = True
    roi_segments, roi_bounds = _collect_roi_segments(
        parsed_segments,
        candidate_bounds,
        padding=max(wall_thickness * 2.5, 40.0),
    )
    metadata["roi_bounds"] = list(roi_bounds)
    metadata["roi_segment_count"] = len(roi_segments)
    if not roi_segments:
        metadata["detection_reason"] = "empty_roi_segments"
        return None, metadata

    resolution = max(wall_thickness / 10.0, 4.0)
    image, render_meta = _render_roi_to_image(
        roi_segments,
        roi_bounds,
        padding=0.0,
        resolution=resolution,
    )
    metadata["resolution_mm_per_px"] = render_meta["resolution_mm_per_px"]

    detection = _detect_door_pattern(
        image=image,
        segments=roi_segments,
        candidate_bounds=candidate_bounds,
        wall_thickness=wall_thickness,
        roi_bounds=roi_bounds,
        resolution=render_meta["resolution_mm_per_px"],
    )
    if detection is None:
        metadata["detection_reason"] = "no_door_pattern"
        return None, metadata

    metadata["arc_segment_count"] = _count_arc_segments(roi_segments, detection.arc_group_id)
    metadata["frame_segment_count"] = detection.frame_segment_count
    metadata["detection_score"] = detection.score
    metadata["pattern_type"] = detection.pattern_type

    cleanup_candidates: List[CleanupCandidate] = []

    raw_cleanup = _cleanup_with_detection_mask(
        polygon=polygon,
        mask_polygon=detection.mask_polygon,
    )
    if raw_cleanup is not None:
        cleanup_candidates.append(raw_cleanup)

    local_cleanup = _cleanup_with_local_opening(
        polygon=polygon,
        candidate_bounds=candidate_bounds,
        wall_thickness=wall_thickness,
    )
    if local_cleanup is not None:
        cleanup_candidates.append(local_cleanup)

    if not cleanup_candidates:
        metadata["detection_reason"] = "empty_difference"
        return None, metadata

    evaluated_candidates = [
        (
            candidate,
            _relax_cleanup_guards(
                candidate=candidate,
                candidate_bounds=candidate_bounds,
                wall_thickness=wall_thickness,
            ),
        )
        for candidate in cleanup_candidates
    ]
    accepted_candidates = [
        (candidate, guard_reasons)
        for candidate, guard_reasons in evaluated_candidates
        if not guard_reasons
    ]
    if accepted_candidates:
        best_cleanup, effective_guard_reasons = max(
            accepted_candidates,
            key=lambda item: item[0].area_delta,
        )
    else:
        best_cleanup, effective_guard_reasons = max(
            evaluated_candidates,
            key=lambda item: item[0].area_delta,
        )
    metadata["guard_reasons"] = effective_guard_reasons
    metadata["cleanup_method"] = best_cleanup.method
    metadata["cleanup_radius"] = best_cleanup.cleanup_radius
    metadata["area_delta"] = best_cleanup.area_delta

    if best_cleanup.area_delta <= max(wall_thickness * wall_thickness * 0.2, 100.0):
        metadata["detection_reason"] = "no_effective_change"
        return None, metadata
    if effective_guard_reasons:
        metadata["detection_reason"] = "guard_rejected"
        return None, metadata

    metadata["applied"] = True
    metadata["detection_reason"] = "door_pattern_removed"
    return best_cleanup.polygon, metadata


def _render_roi_to_image(
    segments: List[Segment],
    roi: Bounds,
    padding: float,
    resolution: float,
) -> Tuple[np.ndarray, Dict]:
    """
    Rasterize ROI linework to a grayscale image.
    """
    roi_bounds = _expand_bounds(roi, padding)
    width_mm = max(roi_bounds[2] - roi_bounds[0], resolution)
    height_mm = max(roi_bounds[3] - roi_bounds[1], resolution)
    max_dimension_px = 1200
    effective_resolution = max(
        resolution,
        width_mm / max_dimension_px,
        height_mm / max_dimension_px,
    )
    width_px = max(32, int(math.ceil(width_mm / effective_resolution)) + 8)
    height_px = max(32, int(math.ceil(height_mm / effective_resolution)) + 8)

    image = np.zeros((height_px, width_px), dtype=np.uint8)
    line_thickness = max(1, int(round(max(2.0, 8.0 / effective_resolution))))

    for segment in segments:
        start = _world_to_pixel(
            segment.start.to_2d(),
            roi_bounds,
            effective_resolution,
            height_px,
            width_px,
        )
        end = _world_to_pixel(
            segment.end.to_2d(),
            roi_bounds,
            effective_resolution,
            height_px,
            width_px,
        )
        cv2.line(image, start, end, color=255, thickness=line_thickness, lineType=cv2.LINE_AA)

    if line_thickness > 1:
        kernel = np.ones((line_thickness, line_thickness), np.uint8)
        image = cv2.dilate(image, kernel, iterations=1)

    return image, {
        "resolution_mm_per_px": effective_resolution,
        "roi_bounds": roi_bounds,
        "line_thickness_px": line_thickness,
    }


def _detect_door_pattern(
    image: np.ndarray,
    segments: List[Segment],
    candidate_bounds: Bounds,
    wall_thickness: float,
    roi_bounds: Bounds,
    resolution: float,
) -> Optional[DoorDetectionResult]:
    """
    Score whether the ROI contains a quarter-arc door swing with a small frame.
    """
    if cv2 is None:
        return None

    arc_groups = _group_arc_segments(segments)

    best_detection: Optional[DoorDetectionResult] = None
    for group_id, arc_segments in arc_groups.items():
        if not _arc_group_intersects_candidate(arc_segments, candidate_bounds):
            continue

        sweep_angle = _median_meta_value(arc_segments, "sweep_angle_deg")
        radius = _median_meta_value(arc_segments, "radius")
        if sweep_angle is None or sweep_angle < 40.0 or sweep_angle > 135.0:
            continue
        if radius is None or radius < wall_thickness * 0.5 or radius > wall_thickness * 12.0:
            continue

        ellipse_ratio = _score_arc_group_with_cv(arc_segments, roi_bounds, resolution)
        if ellipse_ratio is None or ellipse_ratio < 0.45:
            continue

        arc_bounds = _segments_bounds(arc_segments)
        frame_segments = _find_frame_segments(segments, arc_segments, wall_thickness)
        if len(frame_segments) < 2:
            continue
        if not _arc_group_touches_candidate_boundary(arc_segments, candidate_bounds, wall_thickness):
            continue

        mask_polygon = _build_mask_polygon(arc_segments + frame_segments, candidate_bounds, wall_thickness)
        if mask_polygon is None or mask_polygon.is_empty:
            continue

        score = ellipse_ratio + min(len(frame_segments), 4) * 0.15
        detection = DoorDetectionResult(
            mask_polygon=mask_polygon,
            arc_group_id=group_id,
            frame_segment_count=len(frame_segments),
            score=score,
            arc_bounds=arc_bounds,
            pattern_type="arc_frame",
        )
        if best_detection is None or detection.score > best_detection.score:
            best_detection = detection

    diagonal_detection = _detect_diagonal_leaf_pattern(
        image=image,
        segments=segments,
        candidate_bounds=candidate_bounds,
        wall_thickness=wall_thickness,
        roi_bounds=roi_bounds,
        resolution=resolution,
    )
    if diagonal_detection is not None and (
        best_detection is None or diagonal_detection.score > best_detection.score
    ):
        best_detection = diagonal_detection

    return best_detection


def _detect_diagonal_leaf_pattern(
    image: np.ndarray,
    segments: Sequence[Segment],
    candidate_bounds: Bounds,
    wall_thickness: float,
    roi_bounds: Bounds,
    resolution: float,
) -> Optional[DoorDetectionResult]:
    frame_segments = _find_candidate_frame_segments(segments, candidate_bounds, wall_thickness)
    if len(frame_segments) < 2:
        return None

    cv_diagonal_score = _score_diagonal_leaf_with_cv(
        image=image,
        roi_bounds=roi_bounds,
        candidate_bounds=candidate_bounds,
        resolution=resolution,
    )
    diagonal_segments = _find_diagonal_leaf_segments(segments, candidate_bounds, wall_thickness)
    if not diagonal_segments:
        return None
    diagonal_score = max(
        cv_diagonal_score,
        _score_diagonal_leaf_segments(diagonal_segments, candidate_bounds),
    )
    if diagonal_score < 0.45:
        return None

    candidate_width = max(candidate_bounds[2] - candidate_bounds[0], 1e-6)
    candidate_height = max(candidate_bounds[3] - candidate_bounds[1], 1e-6)
    aspect_ratio = max(candidate_width, candidate_height) / min(candidate_width, candidate_height)
    if aspect_ratio < 1.6:
        return None

    mask_polygon = _build_mask_polygon(
        list(frame_segments) + diagonal_segments,
        candidate_bounds,
        wall_thickness,
    )
    if mask_polygon is None or mask_polygon.is_empty:
        mask_polygon = box(*_expand_bounds(candidate_bounds, max(wall_thickness * 0.12, 8.0)))

    score = diagonal_score + min(len(frame_segments), 4) * 0.12
    return DoorDetectionResult(
        mask_polygon=mask_polygon,
        arc_group_id=None,
        frame_segment_count=len(frame_segments),
        score=score,
        arc_bounds=candidate_bounds,
        pattern_type="diagonal_frame",
    )


def _build_mask_polygon(
    segments: Sequence[Segment],
    candidate_bounds: Bounds,
    wall_thickness: float,
) -> Optional[Polygon]:
    buffered_lines = [
        LineString([segment.start.to_2d(), segment.end.to_2d()]).buffer(
            max(wall_thickness * 0.18, 6.0),
            cap_style="square",
            join_style="mitre",
        )
        for segment in segments
    ]
    if not buffered_lines:
        return None

    local_union = unary_union(buffered_lines)
    mask = local_union.convex_hull.buffer(
        max(wall_thickness * 0.22, 8.0),
        cap_style="square",
        join_style="mitre",
    ).intersection(
        box(*_expand_bounds(candidate_bounds, max(wall_thickness * 0.12, 8.0)))
    )
    return _select_polygon(mask)


def _cleanup_with_detection_mask(
    polygon: Polygon,
    mask_polygon: Polygon,
) -> Optional[CleanupCandidate]:
    cleaned = polygon.difference(mask_polygon)
    cleaned_polygon = _select_polygon(cleaned)
    if cleaned_polygon is None or cleaned_polygon.is_empty:
        return None
    return _evaluate_cleanup_candidate(
        original=polygon,
        cleaned=cleaned_polygon,
        method="mask_difference",
        cleanup_radius=0.0,
    )


def _cleanup_with_local_opening(
    polygon: Polygon,
    candidate_bounds: Bounds,
    wall_thickness: float,
) -> Optional[CleanupCandidate]:
    roi = box(*_expand_bounds(candidate_bounds, max(wall_thickness * 1.8, 40.0)))
    local = polygon.intersection(roi)
    local_polygon = _select_polygon(local)
    if local_polygon is None or local_polygon.is_empty:
        return None

    cleanup_radius = max(wall_thickness * 1.5, 12.0)
    opened = local_polygon.buffer(-cleanup_radius, join_style="mitre").buffer(
        cleanup_radius,
        join_style="mitre",
    )
    opened = opened.intersection(roi)
    opened_polygon = _select_polygon(opened)
    if opened_polygon is None or opened_polygon.is_empty:
        return None

    merged = polygon.difference(roi).union(opened_polygon)
    merged_polygon = _select_polygon(merged)
    if merged_polygon is None or merged_polygon.is_empty:
        return None

    return _evaluate_cleanup_candidate(
        original=polygon,
        cleaned=merged_polygon,
        method="local_opening",
        cleanup_radius=cleanup_radius,
    )


def _evaluate_cleanup_candidate(
    original: Polygon,
    cleaned: Polygon,
    method: str,
    cleanup_radius: float,
) -> CleanupCandidate:
    area_delta = original.area - cleaned.area
    bbox_ratio = _bbox_area_ratio(cleaned, original)
    area_ratio = cleaned.area / original.area if original.area > 0 else 1.0
    hole_delta = len(cleaned.interiors) - len(original.interiors)
    hull_delta = abs(_convex_hull_ratio(cleaned) - _convex_hull_ratio(original))

    guard_reasons: List[str] = []
    if area_ratio < 0.8:
        guard_reasons.append("area_drop_exceeded")
    if bbox_ratio < 0.9:
        guard_reasons.append("bbox_drop_exceeded")
    if hole_delta != 0:
        guard_reasons.append("hole_count_changed")
    if hull_delta > 0.12:
        guard_reasons.append("hull_ratio_delta_exceeded")

    return CleanupCandidate(
        polygon=cleaned,
        method=method,
        area_delta=area_delta,
        guard_reasons=guard_reasons,
        bbox_ratio=bbox_ratio,
        area_ratio=area_ratio,
        hole_delta=hole_delta,
        hull_delta=hull_delta,
        cleanup_radius=cleanup_radius,
    )


def _collect_roi_segments(
    segments: List[Segment],
    bounds: Bounds,
    padding: float,
) -> Tuple[List[Segment], Bounds]:
    roi_bounds = _expand_bounds(bounds, padding)
    roi_segments = [segment for segment in segments if _segment_overlaps_bounds(segment, roi_bounds)]
    return roi_segments, roi_bounds


def _group_arc_segments(segments: List[Segment]) -> Dict[Optional[int], List[Segment]]:
    groups: Dict[Optional[int], List[Segment]] = {}
    for segment in segments:
        if segment.meta.get("type") != "arc_segment":
            continue
        group_id = segment.meta.get("arc_group_id")
        groups.setdefault(group_id, []).append(segment)
    return groups


def _score_arc_group_with_cv(
    arc_segments: Sequence[Segment],
    roi_bounds: Bounds,
    resolution: float,
) -> Optional[float]:
    if cv2 is None:
        return None

    image, render_meta = _render_roi_to_image(list(arc_segments), roi_bounds, padding=0.0, resolution=resolution)
    if image.size == 0 or cv2.countNonZero(image) == 0:
        return None

    contours, _ = cv2.findContours(image, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None

    contour = max(contours, key=cv2.contourArea)
    if len(contour) < 5:
        return None

    (_, _), (axis_a, axis_b), _ = cv2.fitEllipse(contour)
    major = max(axis_a, axis_b)
    minor = min(axis_a, axis_b)
    if major <= 1e-6:
        return None

    return float(minor / major)


def _find_frame_segments(
    segments: Sequence[Segment],
    arc_segments: Sequence[Segment],
    wall_thickness: float,
) -> List[Segment]:
    arc_group_id = arc_segments[0].meta.get("arc_group_id") if arc_segments else None
    arc_bounds = _expand_bounds(_segments_bounds(arc_segments), max(wall_thickness * 0.8, 20.0))
    arc_start = arc_segments[0].meta.get("arc_start")
    arc_end = arc_segments[-1].meta.get("arc_end")

    frame_segments: List[Segment] = []
    for segment in segments:
        if segment.meta.get("type") == "arc_segment":
            continue
        if arc_group_id is not None and segment.meta.get("arc_group_id") == arc_group_id:
            continue

        length = segment.length()
        if length < wall_thickness * 0.2 or length > wall_thickness * 3.5:
            continue
        if not _segment_overlaps_bounds(segment, arc_bounds):
            continue
        if not _is_axis_aligned(segment):
            continue

        near_start = arc_start is not None and _segment_near_point(segment, tuple(arc_start), wall_thickness * 1.6)
        near_end = arc_end is not None and _segment_near_point(segment, tuple(arc_end), wall_thickness * 1.6)
        if near_start or near_end:
            frame_segments.append(segment)

    unique_frames: List[Segment] = []
    seen = set()
    for segment in frame_segments:
        key = tuple(sorted((segment.start.to_2d(), segment.end.to_2d())))
        if key in seen:
            continue
        seen.add(key)
        unique_frames.append(segment)
    return unique_frames


def _find_candidate_frame_segments(
    segments: Sequence[Segment],
    candidate_bounds: Bounds,
    wall_thickness: float,
) -> List[Segment]:
    search_bounds = _expand_bounds(candidate_bounds, max(wall_thickness * 0.8, 20.0))
    candidate_box = box(*candidate_bounds)
    frame_segments: List[Segment] = []
    for segment in segments:
        if segment.meta.get("type") == "arc_segment":
            continue
        if not _segment_overlaps_bounds(segment, search_bounds):
            continue
        if not _is_axis_aligned(segment):
            continue

        length = segment.length()
        if length < wall_thickness * 0.2 or length > wall_thickness * 3.5:
            continue

        line = LineString([segment.start.to_2d(), segment.end.to_2d()])
        if line.distance(candidate_box) > max(wall_thickness * 0.8, 16.0):
            continue
        frame_segments.append(segment)

    unique_frames: List[Segment] = []
    seen = set()
    for segment in frame_segments:
        key = tuple(sorted((segment.start.to_2d(), segment.end.to_2d())))
        if key in seen:
            continue
        seen.add(key)
        unique_frames.append(segment)
    return unique_frames


def _find_diagonal_leaf_segments(
    segments: Sequence[Segment],
    candidate_bounds: Bounds,
    wall_thickness: float,
) -> List[Segment]:
    search_bounds = _expand_bounds(candidate_bounds, max(wall_thickness * 1.2, 28.0))
    candidate_box = box(*candidate_bounds)
    diagonal_segments: List[Segment] = []
    corners = [
        (candidate_bounds[0], candidate_bounds[1]),
        (candidate_bounds[2], candidate_bounds[1]),
        (candidate_bounds[2], candidate_bounds[3]),
        (candidate_bounds[0], candidate_bounds[3]),
    ]

    for segment in segments:
        if segment.meta.get("type") == "arc_segment":
            continue
        if not _segment_overlaps_bounds(segment, search_bounds):
            continue
        if _is_axis_aligned(segment, tolerance_deg=12.0):
            continue

        length = segment.length()
        candidate_span = max(
            candidate_bounds[2] - candidate_bounds[0],
            candidate_bounds[3] - candidate_bounds[1],
        )
        if length < max(wall_thickness * 3.0, candidate_span * 0.6):
            continue

        line = LineString([segment.start.to_2d(), segment.end.to_2d()])
        if line.distance(candidate_box) > max(wall_thickness * 0.8, 16.0):
            continue

        endpoints = [segment.start.to_2d(), segment.end.to_2d()]
        if min(
            math.hypot(point[0] - corner[0], point[1] - corner[1])
            for point in endpoints
            for corner in corners
        ) > max(wall_thickness * 1.2, 24.0):
            continue

        diagonal_segments.append(segment)
    return diagonal_segments


def _score_diagonal_leaf_with_cv(
    image: np.ndarray,
    roi_bounds: Bounds,
    candidate_bounds: Bounds,
    resolution: float,
) -> float:
    if cv2 is None or image.size == 0:
        return 0.0

    margin_px = max(8, int(round(16.0 / max(resolution, 1e-6))))
    x1, y1 = _world_to_pixel((candidate_bounds[0], candidate_bounds[3]), roi_bounds, resolution, image.shape[0], image.shape[1])
    x2, y2 = _world_to_pixel((candidate_bounds[2], candidate_bounds[1]), roi_bounds, resolution, image.shape[0], image.shape[1])
    left = max(0, min(x1, x2) - margin_px)
    right = min(image.shape[1], max(x1, x2) + margin_px)
    top = max(0, min(y1, y2) - margin_px)
    bottom = min(image.shape[0], max(y1, y2) + margin_px)
    if right - left < 4 or bottom - top < 4:
        return 0.0

    roi_image = image[top:bottom, left:right]
    edges = cv2.Canny(roi_image, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180.0,
        threshold=18,
        minLineLength=max(10, int(round(max(candidate_bounds[2] - candidate_bounds[0], candidate_bounds[3] - candidate_bounds[1]) / max(resolution, 1e-6) * 0.5))),
        maxLineGap=max(6, int(round(12.0 / max(resolution, 1e-6)))),
    )
    if lines is None:
        return 0.0

    best_score = 0.0
    for line in lines[:, 0]:
        x_start, y_start, x_end, y_end = map(float, line)
        dx = x_end - x_start
        dy = y_end - y_start
        length = math.hypot(dx, dy)
        if length <= 1e-6:
            continue
        angle = abs(math.degrees(math.atan2(dy, dx))) % 180.0
        axis_distance = min(
            angle,
            abs(angle - 90.0),
            abs(angle - 180.0),
        )
        if axis_distance < 15.0:
            continue
        score = min(1.0, length / max((right - left), (bottom - top), 1.0))
        if score > best_score:
            best_score = score
    return best_score


def _score_diagonal_leaf_segments(
    diagonal_segments: Sequence[Segment],
    candidate_bounds: Bounds,
) -> float:
    candidate_span = max(
        candidate_bounds[2] - candidate_bounds[0],
        candidate_bounds[3] - candidate_bounds[1],
        1e-6,
    )
    best_score = 0.0
    for segment in diagonal_segments:
        dx = segment.end.x - segment.start.x
        dy = segment.end.y - segment.start.y
        angle = abs(math.degrees(math.atan2(dy, dx))) % 180.0
        axis_distance = min(angle, abs(angle - 90.0), abs(angle - 180.0))
        diagonal_bias = min(1.0, max(0.0, (axis_distance - 15.0) / 30.0))
        length_bias = min(1.0, segment.length() / candidate_span)
        score = 0.35 + 0.35 * diagonal_bias + 0.30 * length_bias
        if score > best_score:
            best_score = score
    return best_score


def _arc_group_intersects_candidate(segments: Sequence[Segment], candidate_bounds: Bounds) -> bool:
    return _bounds_intersect(_segments_bounds(segments), candidate_bounds)


def _arc_group_touches_candidate_boundary(
    arc_segments: Sequence[Segment],
    candidate_bounds: Bounds,
    wall_thickness: float,
) -> bool:
    candidate_box = box(*candidate_bounds)
    line = LineString(_segments_to_polyline_points(arc_segments))
    return line.distance(candidate_box.boundary) <= max(wall_thickness * 0.7, 12.0)


def _segments_to_polyline_points(segments: Sequence[Segment]) -> List[Tuple[float, float]]:
    if not segments:
        return []
    points = [segments[0].start.to_2d()]
    points.extend(segment.end.to_2d() for segment in segments)
    return points


def _segments_bounds(segments: Sequence[Segment]) -> Bounds:
    xs = [coord for segment in segments for coord in (segment.start.x, segment.end.x)]
    ys = [coord for segment in segments for coord in (segment.start.y, segment.end.y)]
    return (min(xs), min(ys), max(xs), max(ys))


def _segment_overlaps_bounds(segment: Segment, bounds: Bounds) -> bool:
    return _bounds_intersect(
        (
            min(segment.start.x, segment.end.x),
            min(segment.start.y, segment.end.y),
            max(segment.start.x, segment.end.x),
            max(segment.start.y, segment.end.y),
        ),
        bounds,
    )


def _bounds_intersect(left: Bounds, right: Bounds) -> bool:
    return not (
        left[2] < right[0]
        or left[0] > right[2]
        or left[3] < right[1]
        or left[1] > right[3]
    )


def _bounds_list_contains_overlap(
    bounds_list: Sequence[Bounds],
    candidate: Bounds,
    overlap_ratio: float,
) -> bool:
    candidate_box = box(*candidate)
    candidate_area = candidate_box.area
    if candidate_area <= 0:
        return True

    for existing in bounds_list:
        overlap_area = candidate_box.intersection(box(*existing)).area
        if overlap_area / candidate_area >= overlap_ratio:
            return True
    return False


def _expand_bounds(bounds: Bounds, padding: float) -> Bounds:
    return (
        bounds[0] - padding,
        bounds[1] - padding,
        bounds[2] + padding,
        bounds[3] + padding,
    )


def _world_to_pixel(
    point: Tuple[float, float],
    roi_bounds: Bounds,
    resolution: float,
    height_px: int,
    width_px: Optional[int] = None,
) -> Tuple[int, int]:
    x = int(round((point[0] - roi_bounds[0]) / resolution)) + 4
    y = int(round((roi_bounds[3] - point[1]) / resolution)) + 4
    if width_px is not None:
        x = max(0, min(width_px - 1, x))
    y = max(0, min(height_px - 1, y))
    return (x, y)


def _median_meta_value(segments: Sequence[Segment], key: str) -> Optional[float]:
    values = [float(segment.meta[key]) for segment in segments if segment.meta.get(key) is not None]
    if not values:
        return None
    values.sort()
    return values[len(values) // 2]


def _segment_near_point(segment: Segment, point: Tuple[float, float], distance_limit: float) -> bool:
    return min(
        math.hypot(segment.start.x - point[0], segment.start.y - point[1]),
        math.hypot(segment.end.x - point[0], segment.end.y - point[1]),
    ) <= distance_limit


def _is_axis_aligned(segment: Segment, tolerance_deg: float = 15.0) -> bool:
    dx = segment.end.x - segment.start.x
    dy = segment.end.y - segment.start.y
    angle = abs(math.degrees(math.atan2(dy, dx))) % 180.0
    return (
        min(angle, abs(180.0 - angle)) <= tolerance_deg
        or abs(angle - 90.0) <= tolerance_deg
    )


def _count_arc_segments(segments: Sequence[Segment], group_id: Optional[int]) -> int:
    return sum(
        1
        for segment in segments
        if segment.meta.get("type") == "arc_segment" and segment.meta.get("arc_group_id") == group_id
    )


def _relax_cleanup_guards(
    candidate: CleanupCandidate,
    candidate_bounds: Bounds,
    wall_thickness: float,
) -> List[str]:
    effective = list(candidate.guard_reasons)
    if effective != ["hole_count_changed"]:
        return effective
    if candidate.method != "local_opening":
        return effective

    roi_area = box(*_expand_bounds(candidate_bounds, max(wall_thickness * 1.8, 40.0))).area
    if roi_area <= 0:
        return effective
    if candidate.area_delta > roi_area * 0.5:
        return effective
    if candidate.bbox_ratio < 0.995:
        return effective
    if candidate.area_ratio < 0.995:
        return effective
    if candidate.hull_delta > 0.03:
        return effective
    return []


def _bbox_area_ratio(polygon: Polygon, baseline: Polygon) -> float:
    baseline_area = _polygon_bbox_area(baseline)
    if baseline_area <= 0:
        return 1.0
    return _polygon_bbox_area(polygon) / baseline_area


def _polygon_bbox_area(polygon: Polygon) -> float:
    min_x, min_y, max_x, max_y = polygon.bounds
    return max(0.0, max_x - min_x) * max(0.0, max_y - min_y)


def _convex_hull_ratio(polygon: Polygon) -> float:
    hull = polygon.convex_hull
    if hull.is_empty or hull.area == 0:
        return 1.0
    return polygon.area / hull.area


def _select_polygon(geometry) -> Optional[Polygon]:
    if geometry is None or geometry.is_empty:
        return None
    if isinstance(geometry, Polygon):
        return geometry
    if isinstance(geometry, MultiPolygon):
        return max(geometry.geoms, key=lambda geom: geom.area)
    if isinstance(geometry, GeometryCollection):
        polygons = [geom for geom in geometry.geoms if isinstance(geom, Polygon)]
        if polygons:
            return max(polygons, key=lambda geom: geom.area)
    return None
