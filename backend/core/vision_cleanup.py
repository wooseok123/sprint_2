"""
Experimental Gemini-assisted cleanup for suspicious boundary protrusions.

This module keeps the final geometry edit in local vector space. Gemini is used
only to judge whether a highlighted ROI looks like a removable artifact such as
door swings, triangular door leaves, or thin whisker-like spikes.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import logging
import math
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib import error as urllib_error
from urllib import request as urllib_request

import numpy as np
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Polygon, box

from core.outline_cv_fallback import (
    _collect_roi_segments,
    _expand_bounds,
    _select_polygon,
    _world_to_pixel,
    collect_cv_candidate_bounds,
)
from core.parser import Segment

try:  # pragma: no cover - optional runtime dependency
    import cv2
except ImportError:  # pragma: no cover - handled at runtime
    cv2 = None


logger = logging.getLogger(__name__)

Bounds = Tuple[float, float, float, float]


@dataclass
class VisionCleanupCandidate:
    kind: str
    source: str
    bounds: Bounds
    mask_polygon: Optional[Polygon] = None
    score_hint: float = 0.0


class GeminiVisionJudge:
    """Minimal REST client for Gemini multimodal judgments."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-2.5-flash",
        timeout_s: float = 10.0,
    ):
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s

    def judge_candidate(
        self,
        *,
        image_bytes: bytes,
        candidate: VisionCleanupCandidate,
        metrics: Dict[str, Any],
    ) -> Dict[str, Any]:
        prompt = self._build_prompt(candidate=candidate, metrics=metrics)
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "topP": 0.95,
                "maxOutputTokens": 256,
            },
        }
        response_payload = self._post_generate_content(payload)
        text = self._extract_text(response_payload)
        if not text:
            raise ValueError("Gemini response did not contain any text parts")
        verdict = self._parse_verdict_json(text)
        verdict["raw_text"] = text
        return verdict

    def _post_generate_content(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )
        request = urllib_request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as exc:  # pragma: no cover - network path
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Gemini HTTP {exc.code}: {body}") from exc
        except urllib_error.URLError as exc:  # pragma: no cover - network path
            raise RuntimeError(f"Gemini request failed: {exc}") from exc

    def _extract_text(self, payload: Dict[str, Any]) -> str:
        for candidate in payload.get("candidates", []):
            content = candidate.get("content", {})
            for part in content.get("parts", []):
                text = part.get("text")
                if text:
                    return text
        return ""

    def _parse_verdict_json(self, text: str) -> Dict[str, Any]:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"Gemini verdict was not valid JSON: {text!r}")

        payload = json.loads(text[start:end + 1])
        decision = str(payload.get("decision", "")).strip().lower()
        if decision not in {"keep", "remove"}:
            raise ValueError(f"Unexpected Gemini decision: {decision!r}")

        confidence_raw = payload.get("confidence", 0.0)
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = 0.0

        return {
            "decision": decision,
            "confidence": max(0.0, min(1.0, confidence)),
            "feature_type": str(payload.get("feature_type", "unclear")).strip().lower() or "unclear",
            "reason": str(payload.get("reason", "")).strip(),
        }

    def _build_prompt(
        self,
        *,
        candidate: VisionCleanupCandidate,
        metrics: Dict[str, Any],
    ) -> str:
        return (
            "You are reviewing a CAD floor-plan boundary overlay.\n"
            "Image legend:\n"
            "- light gray lines: preprocessed DXF linework\n"
            "- red line: current detected exterior boundary\n"
            "- cyan highlight: suspicious outward feature under review\n"
            "- blue box: ROI around the suspicious feature\n\n"
            "Decide whether the cyan-highlighted feature should be REMOVED from the red boundary.\n"
            "Remove only when it is clearly a boundary artifact such as:\n"
            "- a door swing or door leaf shown as a semicircle, triangle, or diagonal flap\n"
            "- a thin whisker/spike caused by leftover linework\n"
            "- a tiny outward bump unsupported by surrounding wall geometry\n\n"
            "Keep when it looks like a legitimate footprint step, balcony, annex, or wall mass.\n"
            "If uncertain, prefer keep.\n\n"
            f"Candidate kind: {candidate.kind}\n"
            f"Candidate source: {candidate.source}\n"
            f"Candidate width_mm: {metrics['width_mm']:.2f}\n"
            f"Candidate height_mm: {metrics['height_mm']:.2f}\n"
            f"Candidate area_mm2: {metrics['area_mm2']:.2f}\n"
            f"Candidate aspect_ratio: {metrics['aspect_ratio']:.2f}\n\n"
            "Respond with JSON only using this schema:\n"
            '{"decision":"keep|remove","confidence":0.0,"feature_type":"door_arc|door_leaf|thin_spike|legit_mass|unclear","reason":"short explanation"}'
        )


def maybe_apply_gemini_vision_cleanup(
    polygon: Polygon,
    reference_segments: List[Segment],
    wall_thickness: float,
    *,
    preferred_bounds: Optional[Bounds] = None,
    judge: Optional[GeminiVisionJudge] = None,
) -> Tuple[Polygon, Dict[str, Any]]:
    """Run experimental Gemini review on suspicious boundary ROIs."""
    metadata: Dict[str, Any] = {
        "enabled": False,
        "attempted": False,
        "applied": False,
        "applied_count": 0,
        "kept_count": 0,
        "candidate_count": 0,
        "model": None,
        "min_confidence": 0.0,
        "skipped_reason": None,
        "attempts": [],
    }

    if cv2 is None:
        metadata["skipped_reason"] = "cv2_unavailable"
        return polygon, metadata
    if polygon is None or polygon.is_empty or wall_thickness <= 0:
        metadata["skipped_reason"] = "invalid_input"
        return polygon, metadata

    enabled = os.getenv("GEMINI_VISION_CLEANUP_ENABLED", "false").strip().lower() == "true"
    if not enabled and judge is None:
        metadata["skipped_reason"] = "disabled"
        return polygon, metadata

    if judge is None:
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            metadata["skipped_reason"] = "missing_api_key"
            return polygon, metadata
        judge = GeminiVisionJudge(
            api_key=api_key,
            model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash",
            timeout_s=float(os.getenv("AI_JUDGE_TIMEOUT_S", "10")),
        )

    min_confidence = float(os.getenv("GEMINI_VISION_MIN_CONFIDENCE", "0.7"))
    max_candidates = max(1, int(os.getenv("GEMINI_VISION_MAX_CANDIDATES", "6")))

    metadata["enabled"] = True
    metadata["model"] = judge.model
    metadata["min_confidence"] = min_confidence

    candidates = collect_vision_cleanup_candidates(
        polygon=polygon,
        reference_segments=reference_segments,
        wall_thickness=wall_thickness,
        preferred_bounds=preferred_bounds,
        max_candidates=max_candidates,
    )
    metadata["candidate_count"] = len(candidates)
    if not candidates:
        metadata["skipped_reason"] = "no_candidates"
        return polygon, metadata

    metadata["attempted"] = True
    current_polygon = polygon

    for candidate in candidates:
        image_bytes, render_meta = render_candidate_overlay_png(
            polygon=current_polygon,
            reference_segments=reference_segments,
            candidate=candidate,
            wall_thickness=wall_thickness,
        )
        candidate_metrics = _candidate_metrics(candidate)
        attempt_meta: Dict[str, Any] = {
            "kind": candidate.kind,
            "source": candidate.source,
            "bounds": [float(value) for value in candidate.bounds],
            "render": render_meta,
            "metrics": candidate_metrics,
            "decision": "keep",
            "confidence": 0.0,
            "feature_type": "unclear",
            "reason": "",
            "raw_text": None,
            "cleanup": None,
            "error": None,
        }

        try:
            verdict = judge.judge_candidate(
                image_bytes=image_bytes,
                candidate=candidate,
                metrics=candidate_metrics,
            )
            attempt_meta.update(verdict)
        except Exception as exc:  # pragma: no cover - network path
            logger.warning("Gemini vision cleanup skipped candidate: %s", exc)
            attempt_meta["error"] = str(exc)
            metadata["attempts"].append(attempt_meta)
            continue

        should_remove = (
            attempt_meta["decision"] == "remove"
            and attempt_meta["confidence"] >= min_confidence
        )
        if not should_remove:
            metadata["kept_count"] += 1
            metadata["attempts"].append(attempt_meta)
            continue

        cleaned_polygon, cleanup_meta = _apply_candidate_cleanup(
            polygon=current_polygon,
            candidate=candidate,
            wall_thickness=wall_thickness,
        )
        attempt_meta["cleanup"] = cleanup_meta
        if cleaned_polygon is None:
            metadata["kept_count"] += 1
            metadata["attempts"].append(attempt_meta)
            continue

        current_polygon = cleaned_polygon
        metadata["applied"] = True
        metadata["applied_count"] += 1
        metadata["attempts"].append(attempt_meta)

    if not metadata["applied"] and metadata["attempted"]:
        metadata["skipped_reason"] = "all_candidates_kept"

    return current_polygon, metadata


def collect_vision_cleanup_candidates(
    *,
    polygon: Polygon,
    reference_segments: List[Segment],
    wall_thickness: float,
    preferred_bounds: Optional[Bounds] = None,
    max_candidates: int = 6,
) -> List[VisionCleanupCandidate]:
    candidates: List[VisionCleanupCandidate] = []
    seen_bounds: List[Bounds] = []

    for feature in _collect_opening_delta_features(polygon, wall_thickness):
        bounds = tuple(float(value) for value in feature.bounds)
        if _contains_overlapping_bounds(seen_bounds, bounds, overlap_ratio=0.6):
            continue
        candidates.append(
            VisionCleanupCandidate(
                kind="thin_feature",
                source="opening_delta",
                bounds=bounds,
                mask_polygon=feature,
                score_hint=float(feature.area),
            )
        )
        seen_bounds.append(bounds)
        if len(candidates) >= max_candidates:
            return candidates

    arc_candidates = collect_cv_candidate_bounds(
        polygon=polygon,
        parsed_segments=reference_segments,
        wall_thickness=wall_thickness,
        preferred_bounds=preferred_bounds,
        max_candidates=max_candidates,
    )
    for bounds in arc_candidates:
        bounds = tuple(float(value) for value in bounds)
        if _contains_overlapping_bounds(seen_bounds, bounds, overlap_ratio=0.55):
            continue
        source = "bridge_preferred" if preferred_bounds and _bounds_iou(bounds, preferred_bounds) > 0.4 else "arc_roi"
        candidates.append(
            VisionCleanupCandidate(
                kind="door_like",
                source=source,
                bounds=bounds,
                mask_polygon=None,
                score_hint=_bounds_area(bounds),
            )
        )
        seen_bounds.append(bounds)
        if len(candidates) >= max_candidates:
            break

    candidates.sort(key=lambda item: item.score_hint, reverse=True)
    return candidates[:max_candidates]


def render_candidate_overlay_png(
    *,
    polygon: Polygon,
    reference_segments: Sequence[Segment],
    candidate: VisionCleanupCandidate,
    wall_thickness: float,
) -> Tuple[bytes, Dict[str, Any]]:
    padding = max(wall_thickness * 2.4, 60.0)
    roi_segments, roi_bounds = _collect_roi_segments(reference_segments, candidate.bounds, padding=padding)
    render_bounds = _expand_bounds(candidate.bounds, padding)

    width_mm = max(render_bounds[2] - render_bounds[0], 1.0)
    height_mm = max(render_bounds[3] - render_bounds[1], 1.0)
    target_dimension_px = 720.0
    resolution = max(width_mm, height_mm) / target_dimension_px
    resolution = max(1.5, min(8.0, resolution))

    width_px = max(160, int(math.ceil(width_mm / resolution)) + 12)
    height_px = max(160, int(math.ceil(height_mm / resolution)) + 12)
    image = np.full((height_px, width_px, 3), 255, dtype=np.uint8)

    for segment in roi_segments:
        start = _world_to_pixel(segment.start.to_2d(), render_bounds, resolution, height_px, width_px)
        end = _world_to_pixel(segment.end.to_2d(), render_bounds, resolution, height_px, width_px)
        cv2.line(image, start, end, color=(175, 175, 175), thickness=2, lineType=cv2.LINE_AA)

    if candidate.mask_polygon is not None:
        _fill_polygon(image, candidate.mask_polygon, render_bounds, resolution, color=(255, 240, 170))
        _stroke_polygon(image, candidate.mask_polygon, render_bounds, resolution, color=(0, 200, 220), thickness=3)
    else:
        _draw_bounds(image, candidate.bounds, render_bounds, resolution, color=(0, 200, 220), thickness=3)

    _stroke_polygon(image, polygon, render_bounds, resolution, color=(50, 60, 225), thickness=4)
    _draw_bounds(image, candidate.bounds, render_bounds, resolution, color=(215, 110, 30), thickness=2)

    success, encoded = cv2.imencode(".png", image)
    if not success:  # pragma: no cover - cv2 runtime failure
        raise RuntimeError("Failed to encode Gemini ROI overlay image")

    return encoded.tobytes(), {
        "roi_bounds": [float(value) for value in render_bounds],
        "roi_segment_count": len(roi_segments),
        "resolution_mm_per_px": resolution,
        "width_px": width_px,
        "height_px": height_px,
    }


def _collect_opening_delta_features(
    polygon: Polygon,
    wall_thickness: float,
) -> List[Polygon]:
    cleanup_radius = max(8.0, min(wall_thickness * 0.45, 36.0))
    opened = polygon.buffer(-cleanup_radius, join_style="mitre").buffer(
        cleanup_radius,
        join_style="mitre",
    )
    opened_polygon = _select_polygon(opened)
    if opened_polygon is None or opened_polygon.is_empty:
        return []

    delta = polygon.difference(opened_polygon)
    features = _collect_polygons(delta)
    scored: List[Tuple[float, Polygon]] = []
    for feature in features:
        if feature.is_empty or feature.area <= 0:
            continue
        width = feature.bounds[2] - feature.bounds[0]
        height = feature.bounds[3] - feature.bounds[1]
        min_dim = min(width, height)
        max_dim = max(width, height)
        if feature.area < max(40.0, wall_thickness * wall_thickness * 0.15):
            continue
        if feature.area > wall_thickness * wall_thickness * 180.0:
            continue
        if min_dim > max(wall_thickness * 2.0, 80.0):
            continue
        aspect_ratio = max_dim / max(min_dim, 1e-6)
        score = feature.area * min(4.0, aspect_ratio)
        scored.append((score, feature))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [feature for _, feature in scored]


def _apply_candidate_cleanup(
    *,
    polygon: Polygon,
    candidate: VisionCleanupCandidate,
    wall_thickness: float,
) -> Tuple[Optional[Polygon], Dict[str, Any]]:
    cleanup_attempts: List[Dict[str, Any]] = []
    best_polygon: Optional[Polygon] = None
    best_meta: Optional[Dict[str, Any]] = None

    if candidate.mask_polygon is not None:
        padded_mask = candidate.mask_polygon.buffer(
            max(wall_thickness * 0.08, 2.0),
            join_style="mitre",
        )
        cleaned = _select_polygon(polygon.difference(padded_mask))
        if cleaned is not None and not cleaned.is_empty:
            meta = _evaluate_cleanup_result(
                original=polygon,
                cleaned=cleaned,
                method="mask_difference",
            )
            cleanup_attempts.append(meta)
            if not meta["guard_reasons"]:
                best_polygon = cleaned
                best_meta = meta

    local_cleaned = _apply_local_opening_cleanup(
        polygon=polygon,
        bounds=candidate.bounds,
        wall_thickness=wall_thickness,
    )
    if local_cleaned is not None:
        cleaned_polygon, meta = local_cleaned
        cleanup_attempts.append(meta)
        if not meta["guard_reasons"] and (
            best_meta is None or meta["area_delta"] > best_meta["area_delta"]
        ):
            best_polygon = cleaned_polygon
            best_meta = meta

    cleanup_meta = {
        "attempt_count": len(cleanup_attempts),
        "attempts": cleanup_attempts,
        "selected_method": best_meta["method"] if best_meta else None,
    }
    return best_polygon, cleanup_meta


def _apply_local_opening_cleanup(
    *,
    polygon: Polygon,
    bounds: Bounds,
    wall_thickness: float,
) -> Optional[Tuple[Polygon, Dict[str, Any]]]:
    roi = box(*_expand_bounds(bounds, max(wall_thickness * 1.6, 36.0)))
    local = _select_polygon(polygon.intersection(roi))
    if local is None or local.is_empty:
        return None

    cleanup_radius = max(12.0, min(wall_thickness * 1.1, 80.0))
    opened = local.buffer(-cleanup_radius, join_style="mitre").buffer(
        cleanup_radius,
        join_style="mitre",
    )
    opened_polygon = _select_polygon(opened.intersection(roi))
    if opened_polygon is None or opened_polygon.is_empty:
        return None

    merged = polygon.difference(roi).union(opened_polygon)
    merged_polygon = _select_polygon(merged)
    if merged_polygon is None or merged_polygon.is_empty:
        return None

    meta = _evaluate_cleanup_result(
        original=polygon,
        cleaned=merged_polygon,
        method="local_opening",
        cleanup_radius=cleanup_radius,
    )
    return merged_polygon, meta


def _evaluate_cleanup_result(
    *,
    original: Polygon,
    cleaned: Polygon,
    method: str,
    cleanup_radius: float = 0.0,
) -> Dict[str, Any]:
    area_delta = original.area - cleaned.area
    area_ratio = cleaned.area / original.area if original.area > 0 else 1.0
    bbox_ratio = _bbox_area(cleaned.bounds) / max(_bbox_area(original.bounds), 1.0)
    hole_delta = len(cleaned.interiors) - len(original.interiors)
    hull_delta = abs(_convex_hull_ratio(cleaned) - _convex_hull_ratio(original))

    guard_reasons: List[str] = []
    if area_delta <= 0:
        guard_reasons.append("no_effective_change")
    if area_ratio < 0.82:
        guard_reasons.append("area_drop_exceeded")
    if bbox_ratio < 0.90:
        guard_reasons.append("bbox_drop_exceeded")
    if hole_delta != 0:
        guard_reasons.append("hole_count_changed")
    if hull_delta > 0.14:
        guard_reasons.append("hull_ratio_delta_exceeded")

    return {
        "method": method,
        "cleanup_radius": cleanup_radius,
        "area_delta": area_delta,
        "area_ratio": area_ratio,
        "bbox_ratio": bbox_ratio,
        "hole_delta": hole_delta,
        "hull_ratio_delta": hull_delta,
        "guard_reasons": guard_reasons,
    }


def _candidate_metrics(candidate: VisionCleanupCandidate) -> Dict[str, float]:
    width = candidate.bounds[2] - candidate.bounds[0]
    height = candidate.bounds[3] - candidate.bounds[1]
    area = candidate.mask_polygon.area if candidate.mask_polygon is not None else _bounds_area(candidate.bounds)
    return {
        "width_mm": width,
        "height_mm": height,
        "area_mm2": area,
        "aspect_ratio": max(width, height) / max(min(width, height), 1e-6),
    }


def _fill_polygon(
    image: np.ndarray,
    polygon: Polygon,
    roi_bounds: Bounds,
    resolution: float,
    *,
    color: Tuple[int, int, int],
) -> None:
    points = _polygon_to_cv_points(polygon.exterior.coords, roi_bounds, resolution, image.shape[0], image.shape[1])
    if points is None:
        return
    overlay = image.copy()
    cv2.fillPoly(overlay, [points], color=color)
    cv2.addWeighted(overlay, 0.35, image, 0.65, 0.0, dst=image)


def _stroke_polygon(
    image: np.ndarray,
    polygon: Polygon,
    roi_bounds: Bounds,
    resolution: float,
    *,
    color: Tuple[int, int, int],
    thickness: int,
) -> None:
    points = _polygon_to_cv_points(polygon.exterior.coords, roi_bounds, resolution, image.shape[0], image.shape[1])
    if points is not None:
        cv2.polylines(image, [points], isClosed=True, color=color, thickness=thickness, lineType=cv2.LINE_AA)
    for interior in polygon.interiors:
        inner_points = _polygon_to_cv_points(interior.coords, roi_bounds, resolution, image.shape[0], image.shape[1])
        if inner_points is not None:
            cv2.polylines(image, [inner_points], isClosed=True, color=color, thickness=max(1, thickness - 1), lineType=cv2.LINE_AA)


def _draw_bounds(
    image: np.ndarray,
    bounds: Bounds,
    roi_bounds: Bounds,
    resolution: float,
    *,
    color: Tuple[int, int, int],
    thickness: int,
) -> None:
    top_left = _world_to_pixel((bounds[0], bounds[3]), roi_bounds, resolution, image.shape[0], image.shape[1])
    bottom_right = _world_to_pixel((bounds[2], bounds[1]), roi_bounds, resolution, image.shape[0], image.shape[1])
    cv2.rectangle(image, top_left, bottom_right, color=color, thickness=thickness, lineType=cv2.LINE_AA)


def _polygon_to_cv_points(
    coords,
    roi_bounds: Bounds,
    resolution: float,
    height_px: int,
    width_px: int,
) -> Optional[np.ndarray]:
    points = [
        _world_to_pixel((float(x), float(y)), roi_bounds, resolution, height_px, width_px)
        for x, y in coords
    ]
    if len(points) < 3:
        return None
    return np.array(points, dtype=np.int32)


def _collect_polygons(geometry) -> List[Polygon]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        return [geom for geom in geometry.geoms if isinstance(geom, Polygon)]
    return []


def _contains_overlapping_bounds(
    bounds_list: Sequence[Bounds],
    candidate: Bounds,
    *,
    overlap_ratio: float,
) -> bool:
    return any(_bounds_iou(existing, candidate) >= overlap_ratio for existing in bounds_list)


def _bounds_iou(left: Bounds, right: Bounds) -> float:
    left_box = box(*left)
    right_box = box(*right)
    union_area = left_box.union(right_box).area
    if union_area <= 0:
        return 0.0
    return left_box.intersection(right_box).area / union_area


def _bounds_area(bounds: Bounds) -> float:
    return max(0.0, bounds[2] - bounds[0]) * max(0.0, bounds[3] - bounds[1])


def _bbox_area(bounds: Bounds) -> float:
    return _bounds_area(bounds)


def _convex_hull_ratio(polygon: Polygon) -> float:
    hull = polygon.convex_hull
    if hull.is_empty or hull.area <= 0:
        return 1.0
    return polygon.area / hull.area
