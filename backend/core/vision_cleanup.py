"""
Experimental Gemini-assisted cleanup for suspicious boundary protrusions.

This module keeps the final geometry edit in local vector space. Gemini is used
only on structured text features derived from the extracted outline and its
local candidate geometry.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
import os
from statistics import median
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib import error as urllib_error
from urllib import request as urllib_request

from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box

from core.parser import Segment


logger = logging.getLogger(__name__)

Bounds = Tuple[float, float, float, float]


@dataclass
class VisionCleanupCandidate:
    kind: str
    source: str
    bounds: Bounds
    mask_polygon: Optional[Polygon] = None
    score_hint: float = 0.0
    span_start_idx: Optional[int] = None
    span_end_idx: Optional[int] = None
    span_reason: str = ""
    span_confidence: float = 0.0
    span_feature_hint: str = ""
    span_vertices: Tuple[Tuple[float, float], ...] = ()


class GeminiVisionJudge:
    """Minimal REST client for Gemini structured-feature judgments."""

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
        candidate: VisionCleanupCandidate,
        feature_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        prompt = self._build_prompt(candidate=candidate, feature_payload=feature_payload)
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "topP": 0.95,
                "maxOutputTokens": 256,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        response_payload = self._post_generate_content(payload)
        text = self._extract_text(response_payload)
        if not text:
            raise ValueError("Gemini response did not contain any text parts")
        verdict = self._parse_verdict_json(text)
        verdict["raw_text"] = text
        return verdict

    def propose_suspicious_spans(
        self,
        *,
        outline_payload: Dict[str, Any],
        max_spans: int,
    ) -> List[Dict[str, Any]]:
        prompt = self._build_global_scan_prompt(outline_payload=outline_payload, max_spans=max_spans)
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "topP": 0.95,
                "maxOutputTokens": 512,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        response_payload = self._post_generate_content(payload)
        text = self._extract_text(response_payload)
        if not text:
            raise ValueError("Gemini response did not contain any text parts")
        spans = self._parse_suspicious_spans_json(text)
        for span in spans:
            span["raw_text"] = text
        return spans

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
        if decision not in {"keep", "remove", "uncertain"}:
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
        feature_payload: Dict[str, Any],
    ) -> str:
        system_prompt = (
            "You are a conservative CAD exterior-boundary cleanup judge.\n"
            "You receive only structured geometric features for one suspicious outward boundary candidate.\n"
            "Decide whether the candidate should be removed from the exterior boundary.\n"
            "Remove only when the feature is clearly an artifact such as:\n"
            "- a thin spike or whisker\n"
            "- a door swing, door leaf, or triangular flap\n"
            "- a smooth fan-shaped, circular, or arc-like bulge where connecting the endpoints with a straight chord would create a more natural boundary\n"
            "- a tiny outward detour unsupported by surrounding wall mass\n"
            "Keep when the feature could plausibly be a legitimate footprint step, balcony, annex, or wall offset.\n"
            "If the evidence is mixed or insufficient, return uncertain.\n"
            "Never infer visual cues that are not present in the feature JSON.\n"
            "When chain_vs_chord metrics are present, use them heavily.\n"
            "Prefer conservative decisions.\n"
        )
        user_payload = {
            "candidate_kind": candidate.kind,
            "candidate_source": candidate.source,
            "feature_summary": feature_payload,
        }
        return (
            f"{system_prompt}\n"
            "Candidate feature JSON:\n"
            f"{json.dumps(user_payload, indent=2, sort_keys=True)}\n\n"
            "Respond with JSON only using this schema:\n"
            '{"decision":"keep|remove|uncertain","confidence":0.0,"feature_type":"door_arc|door_leaf|thin_spike|smooth_bulge|triangle_flap|legit_mass|unclear","reason":"short explanation"}'
        )

    def _build_global_scan_prompt(
        self,
        *,
        outline_payload: Dict[str, Any],
        max_spans: int,
    ) -> str:
        compact_payload = _compact_outline_payload_for_llm(outline_payload)
        system_prompt = (
            "You are a conservative CAD exterior-boundary anomaly scanner.\n"
            "You receive a simplified exterior boundary summary for a floor-plan footprint.\n"
            "Your task is to identify outward spans that may be artifacts.\n"
            "Examples include thin spikes, triangular door-leaf flaps, door-swing bulges, smooth fan-shaped or circular protrusions, or small unsupported outward detours.\n"
            "Flag spans where replacing the span by a straight chord between its endpoints would likely create a more natural exterior boundary.\n"
            "Do not flag normal footprint steps, balconies, or plausible wall offsets unless they look clearly anomalous.\n"
            "Prefer conservative results. If unsure, return fewer spans.\n"
            "Indices refer to the simplified_exterior_vertices list.\n"
            "Return at most the requested number of spans.\n"
            "Do not use markdown fences.\n"
        )
        return (
            f"{system_prompt}\n"
            "Outline summary JSON:\n"
            f"{json.dumps(compact_payload, separators=(',', ':'), sort_keys=True)}\n\n"
            f"Respond with JSON only using this schema (max {max_spans} items):\n"
            '{"suspicious_spans":[{"start_idx":0,"end_idx":0,"confidence":0.0,"feature_hint":"thin_spike|door_like|triangle_flap|smooth_bulge|unclear","reason":"short explanation"}]}'
        )

    def _parse_suspicious_spans_json(self, text: str) -> List[Dict[str, Any]]:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"Gemini span proposal was not valid JSON: {text!r}")

        payload = json.loads(text[start:end + 1])
        raw_spans = payload.get("suspicious_spans", [])
        if not isinstance(raw_spans, list):
            raise ValueError(f"Unexpected suspicious_spans payload: {raw_spans!r}")

        spans: List[Dict[str, Any]] = []
        for item in raw_spans:
            if not isinstance(item, dict):
                continue
            try:
                start_idx = int(item.get("start_idx"))
                end_idx = int(item.get("end_idx"))
            except (TypeError, ValueError):
                continue
            confidence_raw = item.get("confidence", 0.0)
            try:
                confidence = float(confidence_raw)
            except (TypeError, ValueError):
                confidence = 0.0
            feature_hint = str(item.get("feature_hint", "unclear")).strip().lower() or "unclear"
            spans.append(
                {
                    "start_idx": start_idx,
                    "end_idx": end_idx,
                    "confidence": max(0.0, min(1.0, confidence)),
                    "feature_hint": feature_hint,
                    "reason": str(item.get("reason", "")).strip(),
                }
            )
        return spans


def maybe_apply_gemini_vision_cleanup(
    polygon: Polygon,
    reference_segments: List[Segment],
    wall_thickness: float,
    *,
    preferred_bounds: Optional[Bounds] = None,
    judge: Optional[GeminiVisionJudge] = None,
) -> Tuple[Polygon, Dict[str, Any]]:
    """Run experimental Gemini review on suspicious vector-space candidates."""
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
        "initial_boundary_exterior": _polygon_exterior_coords_or_none(polygon),
        "final_boundary_exterior": _polygon_exterior_coords_or_none(polygon),
        "global_scan": {
            "attempted": False,
            "proposed_count": 0,
            "accepted_count": 0,
            "outline_summary": None,
            "proposals": [],
            "error": None,
        },
    }

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
    max_global_spans = max(1, min(max_candidates, int(os.getenv("GEMINI_VISION_GLOBAL_MAX_SPANS", "5"))))

    metadata["enabled"] = True
    metadata["model"] = judge.model
    metadata["min_confidence"] = min_confidence

    global_candidates, global_scan_meta = _collect_global_outline_candidates(
        polygon=polygon,
        wall_thickness=wall_thickness,
        judge=judge,
        max_candidates=max_global_spans,
    )
    metadata["global_scan"] = global_scan_meta

    heuristic_candidates = collect_vision_cleanup_candidates(
        polygon=polygon,
        reference_segments=reference_segments,
        wall_thickness=wall_thickness,
        preferred_bounds=preferred_bounds,
        max_candidates=max_candidates,
    )
    candidates = _merge_cleanup_candidates(global_candidates, heuristic_candidates, max_candidates=max_candidates)
    metadata["candidate_count"] = len(candidates)
    if not candidates:
        metadata["skipped_reason"] = "no_candidates"
        return polygon, metadata

    metadata["attempted"] = True
    current_polygon = polygon

    for candidate in candidates:
        candidate_metrics = _candidate_feature_payload(
            polygon=current_polygon,
            reference_segments=reference_segments,
            candidate=candidate,
            wall_thickness=wall_thickness,
        )
        attempt_meta: Dict[str, Any] = {
            "kind": candidate.kind,
            "source": candidate.source,
            "bounds": [float(value) for value in candidate.bounds],
            "highlight_ring": _polygon_ring_or_none(candidate.mask_polygon),
            "before_boundary_exterior": _polygon_exterior_coords_or_none(current_polygon),
            "after_boundary_exterior": None,
            "feature_payload": candidate_metrics,
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
                candidate=candidate,
                feature_payload=candidate_metrics,
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
        attempt_meta["after_boundary_exterior"] = _polygon_exterior_coords_or_none(current_polygon)
        metadata["attempts"].append(attempt_meta)

    if not metadata["applied"] and metadata["attempted"]:
        metadata["skipped_reason"] = "all_candidates_kept"

    metadata["final_boundary_exterior"] = _polygon_exterior_coords_or_none(current_polygon)
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

    candidates.sort(key=lambda item: item.score_hint, reverse=True)
    return candidates[:max_candidates]


def _collect_global_outline_candidates(
    *,
    polygon: Polygon,
    wall_thickness: float,
    judge: Any,
    max_candidates: int,
) -> Tuple[List[VisionCleanupCandidate], Dict[str, Any]]:
    metadata: Dict[str, Any] = {
        "attempted": False,
        "proposed_count": 0,
        "accepted_count": 0,
        "outline_summary": None,
        "proposals": [],
        "error": None,
    }
    if max_candidates <= 0 or polygon is None or polygon.is_empty:
        return [], metadata

    if not hasattr(judge, "propose_suspicious_spans"):
        metadata["error"] = "judge_missing_global_scan_method"
        return [], metadata

    outline_summary = _build_outline_scan_payload(polygon=polygon, wall_thickness=wall_thickness)
    metadata["outline_summary"] = outline_summary
    metadata["attempted"] = True

    try:
        proposals = judge.propose_suspicious_spans(
            outline_payload=outline_summary,
            max_spans=max_candidates,
        )
    except Exception as exc:  # pragma: no cover - network path
        logger.warning("Gemini global outline scan skipped: %s", exc)
        metadata["error"] = str(exc)
        return [], metadata

    metadata["proposed_count"] = len(proposals)
    metadata["proposals"] = proposals
    candidates: List[VisionCleanupCandidate] = []
    seen_bounds: List[Bounds] = []
    simplified_vertices = outline_summary["simplified_exterior_vertices"]
    world_origin = tuple(float(value) for value in outline_summary["world_origin"])

    for proposal in proposals:
        candidate = _candidate_from_outline_proposal(
            proposal=proposal,
            simplified_vertices=simplified_vertices,
            world_origin=world_origin,
            wall_thickness=wall_thickness,
        )
        if candidate is None:
            continue
        if _contains_overlapping_bounds(seen_bounds, candidate.bounds, overlap_ratio=0.55):
            continue
        candidates.append(candidate)
        seen_bounds.append(candidate.bounds)
        if len(candidates) >= max_candidates:
            break

    metadata["accepted_count"] = len(candidates)
    return candidates, metadata


def _build_outline_scan_payload(
    *,
    polygon: Polygon,
    wall_thickness: float,
    max_vertices: int = 180,
) -> Dict[str, Any]:
    simplified_coords, tolerance = _simplify_outline_vertices(
        polygon=polygon,
        wall_thickness=wall_thickness,
        max_vertices=max_vertices,
    )
    min_x, min_y, max_x, max_y = polygon.bounds
    width = max_x - min_x
    height = max_y - min_y
    return {
        "world_origin": [float(min_x), float(min_y)],
        "local_bbox": [0.0, 0.0, float(width), float(height)],
        "world_bbox": [float(min_x), float(min_y), float(max_x), float(max_y)],
        "area_mm2": float(polygon.area),
        "perimeter_mm": float(polygon.length),
        "wall_thickness_mm": float(wall_thickness),
        "original_vertex_count": max(0, len(polygon.exterior.coords) - 1),
        "simplified_vertex_count": len(simplified_coords),
        "simplify_tolerance_mm": float(tolerance),
        "simplified_exterior_vertices": [
            {"i": index, "x": float(x - min_x), "y": float(y - min_y)}
            for index, (x, y) in enumerate(simplified_coords)
        ],
    }


def _simplify_outline_vertices(
    *,
    polygon: Polygon,
    wall_thickness: float,
    max_vertices: int,
) -> Tuple[List[Tuple[float, float]], float]:
    diagonal = math.hypot(
        polygon.bounds[2] - polygon.bounds[0],
        polygon.bounds[3] - polygon.bounds[1],
    )
    tolerance = max(wall_thickness * 0.15, diagonal * 0.001, 4.0)
    max_tolerance = max(wall_thickness * 4.0, diagonal * 0.04, tolerance)
    exterior = polygon.exterior

    while True:
        simplified = exterior.simplify(tolerance, preserve_topology=False)
        coords = [(float(x), float(y)) for x, y in simplified.coords[:-1]]
        if len(coords) < 3:
            coords = [(float(x), float(y)) for x, y in exterior.coords[:-1]]
        if len(coords) <= max_vertices or tolerance >= max_tolerance:
            return coords, tolerance
        tolerance *= 1.5


def _compact_outline_payload_for_llm(outline_payload: Dict[str, Any]) -> Dict[str, Any]:
    vertices = outline_payload.get("simplified_exterior_vertices", [])
    return {
        "local_bbox": [round(float(value), 1) for value in outline_payload.get("local_bbox", [0.0, 0.0, 0.0, 0.0])],
        "area_mm2": round(float(outline_payload.get("area_mm2", 0.0)), 1),
        "perimeter_mm": round(float(outline_payload.get("perimeter_mm", 0.0)), 1),
        "wall_thickness_mm": round(float(outline_payload.get("wall_thickness_mm", 0.0)), 1),
        "simplified_vertex_count": int(outline_payload.get("simplified_vertex_count", len(vertices))),
        "simplified_exterior_vertices": [
            {
                "i": int(vertex["i"]),
                "x": round(float(vertex["x"]), 1),
                "y": round(float(vertex["y"]), 1),
            }
            for vertex in vertices
        ],
    }


def _candidate_from_outline_proposal(
    *,
    proposal: Dict[str, Any],
    simplified_vertices: Sequence[Dict[str, float]],
    world_origin: Tuple[float, float],
    wall_thickness: float,
) -> Optional[VisionCleanupCandidate]:
    if not simplified_vertices:
        return None

    try:
        start_idx = int(proposal["start_idx"])
        end_idx = int(proposal["end_idx"])
    except (KeyError, TypeError, ValueError):
        return None

    if start_idx < 0 or end_idx < 0 or start_idx >= len(simplified_vertices) or end_idx >= len(simplified_vertices):
        return None
    if end_idx < start_idx:
        start_idx, end_idx = end_idx, start_idx

    span_vertices = simplified_vertices[start_idx : end_idx + 1]
    if len(span_vertices) < 2:
        return None

    origin_x, origin_y = world_origin
    xs = [vertex["x"] + origin_x for vertex in span_vertices]
    ys = [vertex["y"] + origin_y for vertex in span_vertices]
    padding = max(wall_thickness * 1.4, 20.0)
    bounds = (
        float(min(xs) - padding),
        float(min(ys) - padding),
        float(max(xs) + padding),
        float(max(ys) + padding),
    )
    feature_hint = str(proposal.get("feature_hint", "unclear")).strip().lower()
    kind = "door_like" if "door" in feature_hint else "outline_suspect"
    confidence = float(proposal.get("confidence", 0.0) or 0.0)
    span_length = 0.0
    for left, right in zip(span_vertices, span_vertices[1:]):
        span_length += math.hypot(right["x"] - left["x"], right["y"] - left["y"])
    world_span_vertices = tuple((float(vertex["x"] + origin_x), float(vertex["y"] + origin_y)) for vertex in span_vertices)
    span_mask_polygon = _span_mask_polygon(world_span_vertices)
    if span_mask_polygon is not None and not span_mask_polygon.is_empty:
        bounds = tuple(float(value) for value in span_mask_polygon.bounds)

    return VisionCleanupCandidate(
        kind=kind,
        source="global_outline_scan",
        bounds=bounds,
        mask_polygon=span_mask_polygon,
        score_hint=max(span_length, _bounds_area(bounds)) * max(confidence, 0.25),
        span_start_idx=start_idx,
        span_end_idx=end_idx,
        span_reason=str(proposal.get("reason", "")).strip(),
        span_confidence=max(0.0, min(1.0, confidence)),
        span_feature_hint=feature_hint,
        span_vertices=world_span_vertices,
    )


def _merge_cleanup_candidates(
    global_candidates: Sequence[VisionCleanupCandidate],
    heuristic_candidates: Sequence[VisionCleanupCandidate],
    *,
    max_candidates: int,
) -> List[VisionCleanupCandidate]:
    merged: List[VisionCleanupCandidate] = []
    seen_bounds: List[Bounds] = []
    for candidate in list(global_candidates) + list(heuristic_candidates):
        if _contains_overlapping_bounds(seen_bounds, candidate.bounds, overlap_ratio=0.55):
            continue
        merged.append(candidate)
        seen_bounds.append(candidate.bounds)
        if len(merged) >= max_candidates:
            break

    merged.sort(key=lambda item: item.score_hint, reverse=True)
    return merged[:max_candidates]


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
            meta["guard_reasons"] = _relax_global_span_mask_guards(
                candidate=candidate,
                original=polygon,
                meta=meta,
            )
            cleanup_attempts.append(meta)
            if not meta["guard_reasons"]:
                best_polygon = cleaned
                best_meta = meta

    prefer_mask_cleanup = (
        candidate.source == "global_outline_scan"
        and candidate.mask_polygon is not None
        and best_meta is not None
        and best_meta["method"] == "mask_difference"
    )

    local_cleaned = _apply_local_opening_cleanup(
        polygon=polygon,
        bounds=candidate.bounds,
        wall_thickness=wall_thickness,
    )
    if local_cleaned is not None:
        cleaned_polygon, meta = local_cleaned
        cleanup_attempts.append(meta)
        if not meta["guard_reasons"] and not prefer_mask_cleanup and (
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


def _candidate_feature_payload(
    *,
    polygon: Polygon,
    reference_segments: Sequence[Segment],
    candidate: VisionCleanupCandidate,
    wall_thickness: float,
) -> Dict[str, Any]:
    width = max(candidate.bounds[2] - candidate.bounds[0], 0.0)
    height = max(candidate.bounds[3] - candidate.bounds[1], 0.0)
    bbox_area = _bounds_area(candidate.bounds)
    candidate_area = candidate.mask_polygon.area if candidate.mask_polygon is not None else bbox_area
    min_dim = min(width, height)
    max_dim = max(width, height)

    payload: Dict[str, Any] = {
        "candidate_kind": candidate.kind,
        "candidate_source": candidate.source,
        "candidate_bounds": [float(value) for value in candidate.bounds],
        "global_span_start_idx": candidate.span_start_idx,
        "global_span_end_idx": candidate.span_end_idx,
        "global_span_reason": candidate.span_reason,
        "global_span_confidence": float(candidate.span_confidence),
        "global_span_feature_hint": candidate.span_feature_hint,
        "wall_thickness_mm": float(wall_thickness),
        "width_mm": float(width),
        "height_mm": float(height),
        "min_dim_mm": float(min_dim),
        "max_dim_mm": float(max_dim),
        "bbox_area_mm2": float(bbox_area),
        "candidate_area_mm2": float(candidate_area),
        "aspect_ratio": float(max_dim / max(min_dim, 1e-6)),
        "min_dim_to_wall_ratio": float(min_dim / max(wall_thickness, 1e-6)),
        "max_dim_to_wall_ratio": float(max_dim / max(wall_thickness, 1e-6)),
        "candidate_area_ratio_of_polygon": float(candidate_area / max(polygon.area, 1e-6)),
        "candidate_bbox_area_ratio_of_polygon_bbox": float(bbox_area / max(_bbox_area(polygon.bounds), 1e-6)),
    }

    if candidate.mask_polygon is not None and not candidate.mask_polygon.is_empty:
        payload.update(_mask_polygon_features(candidate.mask_polygon, wall_thickness))

    chain_features = _span_chain_features(candidate, polygon, wall_thickness)
    if chain_features:
        payload["chain_vs_chord"] = chain_features

    payload.update(
        _roi_segment_features(
            reference_segments=reference_segments,
            bounds=candidate.bounds,
            wall_thickness=wall_thickness,
        )
    )
    return payload


def _span_chain_features(
    candidate: VisionCleanupCandidate,
    polygon: Polygon,
    wall_thickness: float,
) -> Optional[Dict[str, Any]]:
    span_vertices = list(candidate.span_vertices or ())
    if len(span_vertices) < 2:
        return None

    start = span_vertices[0]
    end = span_vertices[-1]
    chord_dx = end[0] - start[0]
    chord_dy = end[1] - start[1]
    chord_length = math.hypot(chord_dx, chord_dy)
    detour_length = sum(
        math.hypot(right[0] - left[0], right[1] - left[1])
        for left, right in zip(span_vertices, span_vertices[1:])
    )
    chord_orientation = math.degrees(math.atan2(chord_dy, chord_dx)) if chord_length > 1e-6 else 0.0

    local_origin_x = min(point[0] for point in span_vertices)
    local_origin_y = min(point[1] for point in span_vertices)
    local_vertices = [
        [round(point[0] - local_origin_x, 1), round(point[1] - local_origin_y, 1)]
        for point in span_vertices
    ]
    chord_local = {
        "start": [round(start[0] - local_origin_x, 1), round(start[1] - local_origin_y, 1)],
        "end": [round(end[0] - local_origin_x, 1), round(end[1] - local_origin_y, 1)],
    }

    offsets: List[float] = []
    abs_offsets: List[float] = []
    if chord_length > 1e-6:
        for point in span_vertices[1:-1]:
            signed_offset = ((point[0] - start[0]) * chord_dy - (point[1] - start[1]) * chord_dx) / chord_length
            offsets.append(signed_offset)
            abs_offsets.append(abs(signed_offset))

    dominant_side_ratio = 0.0
    if offsets:
        positive_count = sum(1 for value in offsets if value > 1e-6)
        negative_count = sum(1 for value in offsets if value < -1e-6)
        dominant_side_ratio = max(positive_count, negative_count) / max(len(offsets), 1)

    turning_angles: List[float] = []
    for left, middle, right in zip(span_vertices, span_vertices[1:], span_vertices[2:]):
        vector_a = (middle[0] - left[0], middle[1] - left[1])
        vector_b = (right[0] - middle[0], right[1] - middle[1])
        magnitude_a = math.hypot(*vector_a)
        magnitude_b = math.hypot(*vector_b)
        if magnitude_a <= 1e-6 or magnitude_b <= 1e-6:
            continue
        cross = vector_a[0] * vector_b[1] - vector_a[1] * vector_b[0]
        dot = vector_a[0] * vector_b[0] + vector_a[1] * vector_b[1]
        turning_angles.append(math.degrees(math.atan2(cross, dot)))

    same_turn_ratio = 0.0
    if turning_angles:
        positive_turns = sum(1 for angle in turning_angles if angle > 5.0)
        negative_turns = sum(1 for angle in turning_angles if angle < -5.0)
        same_turn_ratio = max(positive_turns, negative_turns) / max(len(turning_angles), 1)

    enclosed_area = 0.0
    if len(span_vertices) >= 3:
        try:
            enclosed_area = abs(Polygon(span_vertices).area)
        except Exception:
            enclosed_area = 0.0

    max_offset = max(abs_offsets) if abs_offsets else 0.0
    mean_offset = sum(abs_offsets) / len(abs_offsets) if abs_offsets else 0.0
    detour_ratio = detour_length / max(chord_length, 1e-6)

    return {
        "span_vertex_count": len(span_vertices),
        "span_vertices_local": local_vertices,
        "chord_local": chord_local,
        "chord_length_mm": float(chord_length),
        "detour_length_mm": float(detour_length),
        "detour_to_chord_ratio": float(detour_ratio),
        "max_offset_from_chord_mm": float(max_offset),
        "mean_offset_from_chord_mm": float(mean_offset),
        "max_offset_to_wall_ratio": float(max_offset / max(wall_thickness, 1e-6)),
        "dominant_side_ratio": float(dominant_side_ratio),
        "same_turn_ratio": float(same_turn_ratio),
        "turning_angles_deg": [round(angle, 1) for angle in turning_angles],
        "enclosed_area_mm2": float(enclosed_area),
        "enclosed_area_ratio_of_polygon": float(enclosed_area / max(polygon.area, 1e-6)),
        "chord_orientation_deg": float(chord_orientation),
        "looks_like_smooth_bulge": bool(
            len(span_vertices) >= 3
            and detour_ratio >= 1.05
            and max_offset > 1e-3
            and dominant_side_ratio >= 0.8
            and same_turn_ratio >= 0.6
        ),
    }


def _mask_polygon_features(mask_polygon: Polygon, wall_thickness: float) -> Dict[str, Any]:
    perimeter = mask_polygon.length
    hull = mask_polygon.convex_hull
    hull_area = hull.area if not hull.is_empty else 0.0
    min_rect = mask_polygon.minimum_rotated_rectangle
    rect_coords = list(min_rect.exterior.coords)
    rect_sides = []
    for start, end in zip(rect_coords, rect_coords[1:]):
        rect_sides.append(math.hypot(end[0] - start[0], end[1] - start[1]))
    unique_sides = sorted({round(side, 6) for side in rect_sides if side > 1e-6})
    rect_min_dim = unique_sides[0] if unique_sides else 0.0
    rect_max_dim = unique_sides[-1] if unique_sides else 0.0

    return {
        "mask_area_mm2": float(mask_polygon.area),
        "mask_perimeter_mm": float(perimeter),
        "mask_vertex_count": max(0, len(mask_polygon.exterior.coords) - 1),
        "mask_convex_hull_ratio": float(mask_polygon.area / max(hull_area, 1e-6)),
        "mask_bbox_fill_ratio": float(mask_polygon.area / max(_bounds_area(mask_polygon.bounds), 1e-6)),
        "mask_rect_min_dim_mm": float(rect_min_dim),
        "mask_rect_max_dim_mm": float(rect_max_dim),
        "mask_rect_aspect_ratio": float(rect_max_dim / max(rect_min_dim, 1e-6)),
        "neck_width_mm": float(rect_min_dim),
        "neck_width_to_wall_ratio": float(rect_min_dim / max(wall_thickness, 1e-6)),
        "compactness": float((perimeter * perimeter) / max(mask_polygon.area, 1e-6)),
        "is_triangle_like": bool(max(0, len(mask_polygon.exterior.coords) - 1) <= 4),
    }


def _roi_segment_features(
    *,
    reference_segments: Sequence[Segment],
    bounds: Bounds,
    wall_thickness: float,
) -> Dict[str, Any]:
    padding = max(wall_thickness * 1.8, 40.0)
    roi_segments, roi_bounds = _collect_roi_segments(reference_segments, bounds, padding=padding)
    segment_lengths = [segment.length() for segment in roi_segments]
    horizontal_count = 0
    vertical_count = 0
    diagonal_count = 0
    arc_radii: List[float] = []
    arc_sweeps: List[float] = []
    unique_arc_groups = set()

    for segment in roi_segments:
        dx = segment.end.x - segment.start.x
        dy = segment.end.y - segment.start.y
        angle_deg = abs(math.degrees(math.atan2(dy, dx))) % 180.0
        if angle_deg <= 12.0 or angle_deg >= 168.0:
            horizontal_count += 1
        elif 78.0 <= angle_deg <= 102.0:
            vertical_count += 1
        else:
            diagonal_count += 1

        meta = segment.meta or {}
        segment_type = str(meta.get("type", "")).lower()
        if segment_type == "arc":
            radius = meta.get("radius")
            sweep = meta.get("sweep_angle_deg")
            group_id = meta.get("arc_group_id")
            if radius is not None:
                try:
                    arc_radii.append(float(radius))
                except (TypeError, ValueError):
                    pass
            if sweep is not None:
                try:
                    arc_sweeps.append(float(sweep))
                except (TypeError, ValueError):
                    pass
            if group_id is not None:
                unique_arc_groups.add(group_id)

    total_segments = len(roi_segments)
    total_length = sum(segment_lengths)
    return {
        "roi_bounds": [float(value) for value in roi_bounds],
        "roi_segment_count": total_segments,
        "roi_total_segment_length_mm": float(total_length),
        "roi_longest_segment_mm": float(max(segment_lengths) if segment_lengths else 0.0),
        "roi_median_segment_length_mm": float(median(segment_lengths) if segment_lengths else 0.0),
        "roi_horizontal_segment_count": horizontal_count,
        "roi_vertical_segment_count": vertical_count,
        "roi_diagonal_segment_count": diagonal_count,
        "roi_axis_aligned_ratio": float((horizontal_count + vertical_count) / max(total_segments, 1)),
        "roi_arc_segment_count": len(arc_radii),
        "roi_arc_group_count": len(unique_arc_groups),
        "roi_arc_radius_median_mm": float(median(arc_radii) if arc_radii else 0.0),
        "roi_arc_sweep_median_deg": float(median(arc_sweeps) if arc_sweeps else 0.0),
        "roi_has_door_arc_signal": bool(arc_radii and arc_sweeps),
    }


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


def _collect_roi_segments(
    segments: Sequence[Segment],
    bounds: Bounds,
    padding: float,
) -> Tuple[List[Segment], Bounds]:
    roi_bounds = _expand_bounds(bounds, padding)
    roi_segments = [segment for segment in segments if _segment_overlaps_bounds(segment, roi_bounds)]
    return roi_segments, roi_bounds


def _expand_bounds(bounds: Bounds, padding: float) -> Bounds:
    return (
        bounds[0] - padding,
        bounds[1] - padding,
        bounds[2] + padding,
        bounds[3] + padding,
    )


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


def _bounds_area(bounds: Bounds) -> float:
    return max(0.0, bounds[2] - bounds[0]) * max(0.0, bounds[3] - bounds[1])


def _bbox_area(bounds: Bounds) -> float:
    return _bounds_area(bounds)


def _convex_hull_ratio(polygon: Polygon) -> float:
    hull = polygon.convex_hull
    if hull.is_empty or hull.area <= 0:
        return 1.0
    return polygon.area / hull.area


def _relax_global_span_mask_guards(
    *,
    candidate: VisionCleanupCandidate,
    original: Polygon,
    meta: Dict[str, Any],
) -> List[str]:
    guard_reasons = list(meta.get("guard_reasons", []))
    if candidate.source != "global_outline_scan" or candidate.mask_polygon is None:
        return guard_reasons

    allowed = {"bbox_drop_exceeded", "hull_ratio_delta_exceeded"}
    if not guard_reasons or any(reason not in allowed for reason in guard_reasons):
        return guard_reasons

    if meta.get("hole_delta") != 0:
        return guard_reasons
    if meta.get("area_ratio", 0.0) < 0.88:
        return guard_reasons
    if candidate.mask_polygon.area / max(original.area, 1e-6) > 0.12:
        return guard_reasons
    if len(candidate.span_vertices) < 3:
        return guard_reasons

    return []


def _span_mask_polygon(
    span_vertices: Sequence[Tuple[float, float]],
) -> Optional[Polygon]:
    if len(span_vertices) < 3:
        return None

    try:
        polygon = Polygon(span_vertices)
    except Exception:
        return None

    if polygon.is_empty or polygon.area <= 0:
        return None

    if not polygon.is_valid:
        polygon = polygon.buffer(0)
        if polygon.is_empty:
            return None
        polygon = _select_polygon(polygon)
        if polygon is None or polygon.is_empty or polygon.area <= 0:
            return None

    return polygon


def _polygon_ring_or_none(polygon: Optional[Polygon]) -> Optional[List[List[float]]]:
    if polygon is None or polygon.is_empty:
        return None
    return [[float(x), float(y)] for x, y in polygon.exterior.coords]


def _polygon_exterior_coords_or_none(polygon: Optional[Polygon]) -> Optional[List[List[float]]]:
    if polygon is None or polygon.is_empty:
        return None
    return [[float(x), float(y)] for x, y in polygon.exterior.coords]
