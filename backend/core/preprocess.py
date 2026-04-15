"""
DXF preprocessing heuristics.

This stage is intentionally separate from raw DXF parsing so heuristic cleanup
can be enabled, inspected, and tuned without changing extraction behavior.
"""
from collections import deque
from dataclasses import dataclass, field
import math
from typing import Any, Dict, List, Set, Tuple

import numpy as np
from shapely.geometry import LineString

from core.parser import DXFParser, FlattenedEntity, ParsedDXF, Segment


@dataclass
class PreprocessedDXF:
    """Result of the optional preprocessing stage."""
    segments: List[Segment]
    bbox: Dict[str, float]
    preprocessing: Dict[str, Any] = field(default_factory=dict)


class DXFPreprocessor:
    """Apply heuristic cleanup to raw flattened DXF entities."""

    TITLE_BLOCK_GRID_SIZE = 10
    TITLE_BLOCK_MAX_WIDTH_RATIO = 0.45
    TITLE_BLOCK_MAX_HEIGHT_RATIO = 0.45
    TITLE_BLOCK_MAX_AREA_RATIO = 0.22
    TITLE_BLOCK_WALL_SPAN_RATIO = 0.12
    TITLE_BLOCK_EDGE_SPAN_RATIO = 0.15
    TITLE_BLOCK_LINE_SPAN_RATIO = 0.65
    TITLE_BLOCK_MAX_CORNER_COL_SPAN = 4
    TITLE_BLOCK_MAX_CORNER_ROW_SPAN = 4
    ANNOTATION_LAYER_RATIO_THRESHOLD = 0.7
    ANNOTATION_LAYER_KEYWORDS = (
        "dim", "dimension", "annotation", "anno", "leader", "text", "label",
        "note", "center", "centre", "hidden", "hatch", "table", "schedule",
        "title", "tbl", "logo", "stamp", "room-iden", "room_iden", "roomiden",
        "표제", "치수", "중심", "통심", "해치", "일람",
    )
    FORCED_DELETE_LAYER_KEYWORDS = (
        "symbol", "sym", "annotation", "anno", "dimension", "dim", "text", "mtext", "mark",
    )
    ANNOTATION_BLOCK_KEYWORDS = (
        "dim", "arrow", "tick", "leader", "table", "schedule",
        "title", "logo", "stamp", "표제", "일람",
    )
    PROTECTED_BOUNDARY_LAYER_KEYWORDS = (
        "wall", "outline", "outer", "exterior", "facade", "perimeter",
        "wal", "wid", "외벽", "벽체", "윤곽", "외곽",
    )
    ANNOTATION_LINETYPE_KEYWORDS = ("CENTER", "PHANTOM", "HIDDEN", "DASHED", "DASHDOT")
    ANNOTATION_CONTEXT_RADIUS_RATIO = 0.05
    DETACHED_RECTANGLE_MAX_AREA_RATIO = 0.18
    DETACHED_RECTANGLE_CONTACT_TOLERANCE_RATIO = 0.0015
    FRAME_SEED_MIN_AREA_RATIO = 0.7
    FRAME_CANDIDATE_MIN_AREA_RATIO = 0.08
    FRAME_CHAIN_MIN_AREA_RATIO = 0.55
    FRAME_CENTER_TOLERANCE_RATIO = 0.04
    FRAME_ASPECT_TOLERANCE = 0.12
    FRAME_INSET_TOLERANCE_RATIO = 0.02
    FRAME_INNER_MIN_AREA_RATIO = 0.12
    FRAME_INTERIOR_ENTITY_RATIO = 0.02
    FRAME_MIN_INTERIOR_ENTITY_COUNT = 1
    FRAME_KEEP_OVERLAP_RATIO = 0.5
    FRAME_REMOVE_INNERMOST_MIN_INTERIOR_COUNT = 2
    FRAME_KEEP_INNERMOST_MIN_EDGE_CONTACTS = 1
    FRAME_OUTER_WRAPPER_MIN_AREA_RATIO = 0.4
    FRAME_OUTER_EDGE_TOLERANCE_RATIO = 0.03
    FRAME_ALLOWED_STRAY_CONTACTS = 5

    def __init__(self, parser: DXFParser):
        self.parser = parser

    def preprocess(
        self,
        parsed: ParsedDXF,
        *,
        run_isolated_segment_cleanup: bool = False,
    ) -> PreprocessedDXF:
        flattened_entities = parsed.flattened_entities
        if not flattened_entities:
            return PreprocessedDXF(
                segments=[],
                bbox={"minX": 0, "minY": 0, "maxX": 0, "maxY": 0},
                preprocessing={
                    "flattened_entities": 0,
                    "geometry_entities_after_filters": 0,
                    "removed_by_type": 0,
                    "removed_by_annotation": 0,
                    "removed_by_linetype": 0,
                    "removed_by_title_block": 0,
                    "removed_by_border_frame": 0,
                    "removed_detached_rectangles": 0,
                    "removed_short_segments": 0,
                    "removed_isolated_segments": 0,
                },
            )

        removed_ids: Set[int] = set()
        removed_by_type: Set[int] = set()
        removed_by_annotation: Set[int] = set()
        removed_by_linetype: Set[int] = set()
        deferred_annotation_ids: Set[int] = set()
        geometry_candidates: List[Tuple[int, FlattenedEntity]] = []
        drawing_bbox = self.parser._calculate_flattened_bbox(flattened_entities)
        layer_type_stats = self._build_layer_type_stats(flattened_entities)
        short_line_threshold = self._estimate_short_line_threshold(
            flattened_entities,
            drawing_bbox,
            set(),
        )
        annotation_context_centers = self._build_annotation_context_centers(
            flattened_entities,
            layer_type_stats,
        )
        annotation_signal_centers = self._build_annotation_signal_centers(flattened_entities)

        for index, flattened in enumerate(flattened_entities):
            dxftype = flattened.entity.dxftype()

            if dxftype in self.parser.FORCED_DELETE_TYPES:
                removed_ids.add(index)
                removed_by_type.add(index)
                continue

            if self._should_force_remove_layer(flattened.effective_layer):
                removed_ids.add(index)
                removed_by_annotation.add(index)
                continue

            if self._should_remove_annotation_geometry(
                flattened,
                drawing_bbox,
                layer_type_stats,
                short_line_threshold,
                annotation_context_centers,
                annotation_signal_centers,
            ):
                deferred_annotation_ids.add(index)
                continue

            if self.parser._matches_filtered_linetype(flattened.effective_linetype):
                removed_ids.add(index)
                removed_by_linetype.add(index)
                continue

            if dxftype in self.parser.GEOMETRY_ENTITY_TYPES:
                geometry_candidates.append((index, flattened))

        short_line_threshold = self._estimate_short_line_threshold(
            flattened_entities,
            drawing_bbox,
            deferred_annotation_ids | removed_by_linetype,
        )
        structured_line_threshold = self._structured_line_threshold(drawing_bbox, short_line_threshold)

        structural_geometry = [
            (index, flattened)
            for index, flattened in geometry_candidates
            if index not in removed_ids and index not in deferred_annotation_ids
        ]
        work_area_bbox, frame_window_meta = self._resolve_work_area_bbox(
            structural_geometry,
            drawing_bbox,
        )
        removed_by_frame_window: Set[int] = set()
        if work_area_bbox is not None:
            outer_wrapper_indices, outer_wrapper_bboxes = self._collect_outer_wrapper_frames(
                structural_geometry,
                work_area_bbox,
                frame_window_meta,
                drawing_bbox,
            )
            frame_window_meta["outer_wrapper_bboxes"] = outer_wrapper_bboxes
            frame_window_meta["outer_wrapper_indices"] = outer_wrapper_indices
            frame_window_meta["frame_chain_indices"].update(outer_wrapper_indices)

            enclosing_frame_indices, enclosing_frame_bboxes = self._collect_enclosing_work_area_frames(
                structural_geometry,
                work_area_bbox,
                frame_window_meta,
                drawing_bbox,
            )
            frame_window_meta["enclosing_frame_bboxes"] = enclosing_frame_bboxes
            frame_window_meta["enclosing_frame_indices"] = enclosing_frame_indices
            frame_window_meta["frame_chain_indices"].update(enclosing_frame_indices)

            for index, flattened in geometry_candidates:
                if index in removed_ids:
                    continue
                if not self._should_keep_entity_in_work_area(flattened, work_area_bbox):
                    removed_ids.add(index)
                    removed_by_frame_window.add(index)

            for frame_index in frame_window_meta.get("frame_chain_indices", set()):
                flattened = flattened_entities[frame_index]
                if (
                    frame_index in frame_window_meta.get("innermost_frame_indices", set())
                    and (
                        self._is_protected_boundary_layer(flattened.effective_layer)
                        or frame_window_meta.get("innermost_interior_count", 0) < self.FRAME_REMOVE_INNERMOST_MIN_INTERIOR_COUNT
                        or frame_window_meta.get("innermost_edge_contact_count", 0) >= self.FRAME_KEEP_INNERMOST_MIN_EDGE_CONTACTS
                    )
                ):
                    continue
                removed_ids.add(frame_index)
                removed_by_frame_window.add(frame_index)

        title_block_candidate_bbox = self._detect_title_block_candidate_bbox(
            flattened_entities,
            drawing_bbox,
            excluded_ids=deferred_annotation_ids | removed_by_linetype | removed_by_frame_window,
        )
        title_block_bbox, title_block_meta = self._confirm_title_block_bbox(
            flattened_entities,
            drawing_bbox,
            candidate_bbox=title_block_candidate_bbox,
            excluded_ids=deferred_annotation_ids | removed_by_linetype | removed_by_frame_window,
            layer_type_stats=layer_type_stats,
            structured_line_threshold=structured_line_threshold,
        )
        removed_by_title_block: Set[int] = set()
        if title_block_bbox is not None:
            for index, flattened in enumerate(flattened_entities):
                if not self._flattened_entity_within_bbox(flattened, title_block_bbox):
                    continue
                if not self._is_title_block_helper_entity(
                    flattened,
                    title_block_bbox,
                    drawing_bbox,
                    layer_type_stats,
                    structured_line_threshold,
                ):
                    continue
                removed_ids.add(index)
                removed_by_title_block.add(index)

        removed_by_border_frame: Set[int] = set()
        border_frame_bbox = frame_window_meta.get("outermost_frame_bbox")
        border_frame_bboxes = (
            frame_window_meta.get("chain_bboxes", [])
            + frame_window_meta.get("outer_wrapper_bboxes", [])
            + frame_window_meta.get("enclosing_frame_bboxes", [])
        )
        if removed_by_frame_window:
            removed_by_border_frame.update(
                set(frame_window_meta.get("frame_chain_indices", set())) & removed_by_frame_window
            )

        remaining_geometry = [
            (index, flattened)
            for index, flattened in geometry_candidates
            if index not in removed_ids and index not in deferred_annotation_ids
        ]
        detached_rectangle_indices = self._detect_detached_rectangle_indices(remaining_geometry, drawing_bbox)
        for index in detached_rectangle_indices:
            removed_ids.add(index)

        removed_ids.update(deferred_annotation_ids)
        removed_by_annotation.update(deferred_annotation_ids)

        segments: List[Segment] = []
        for index, flattened in geometry_candidates:
            if index in removed_ids:
                continue
            segments.extend(self.parser._process_flattened_entity(flattened))

        segments, short_segment_meta = self.parser._remove_short_segments(segments, drawing_bbox)
        if run_isolated_segment_cleanup:
            segments, isolation_meta = self.parser._remove_isolated_segments(segments, drawing_bbox)
        else:
            isolation_meta = {"removed_segments": 0, "removed_components": 0}
        bbox = self.parser._calculate_bbox(segments)

        preprocessing = {
            "flattened_entities": len(flattened_entities),
            "geometry_entities_after_filters": len([
                1 for index, _ in geometry_candidates if index not in removed_ids
            ]),
            "removed_by_type": len(removed_by_type),
            "removed_by_annotation": len(removed_by_annotation),
            "removed_by_linetype": len(removed_by_linetype),
            "removed_by_title_block": len(removed_by_title_block),
            "removed_by_border_frame": len(removed_by_border_frame),
            "removed_by_frame_window": len(removed_by_frame_window),
            "removed_detached_rectangles": len(detached_rectangle_indices),
            "removed_short_segments": short_segment_meta["removed_segments"],
            "removed_isolated_segments": isolation_meta["removed_segments"],
            "removed_isolated_components": isolation_meta["removed_components"],
            "isolated_segment_cleanup_deferred": not run_isolated_segment_cleanup,
            "short_segment_threshold_mm": short_segment_meta["threshold"],
            "title_block_candidate_bbox": title_block_candidate_bbox,
            "title_block_bbox": title_block_bbox,
            "title_block_confirmed": title_block_bbox is not None,
            "title_block_debug": title_block_meta,
            "border_frame_bbox": border_frame_bbox,
            "border_frame_bboxes": border_frame_bboxes,
            "work_area_bbox": work_area_bbox,
            "frame_window_debug": frame_window_meta,
            "drawing_bbox_before_cleanup": drawing_bbox,
            "segments_after_preprocessing": len(segments),
        }

        return PreprocessedDXF(
            segments=segments,
            bbox=bbox,
            preprocessing=preprocessing,
        )

    def _resolve_work_area_bbox(
        self,
        geometry_entities: List[Tuple[int, FlattenedEntity]],
        drawing_bbox: Dict[str, float],
    ) -> Tuple[Dict[str, float] | None, Dict[str, Any]]:
        candidates = self._collect_frame_rectangle_candidates(geometry_entities)
        if not candidates:
            return drawing_bbox, {
                "candidate_count": 0,
                "chain_count": 0,
                "selected_chain_length": 0,
                "selected_chain_bboxes": [],
                "outermost_frame_bbox": None,
                "innermost_frame_bbox": None,
                "frame_chain_indices": set(),
                "innermost_frame_indices": set(),
                "fallback_to_drawing_bbox": True,
                "reason": "no_candidates",
            }

        drawing_area = self._bbox_area(drawing_bbox)
        if drawing_area <= 0:
            return drawing_bbox, {
                "candidate_count": len(candidates),
                "chain_count": 0,
                "selected_chain_length": 0,
                "selected_chain_bboxes": [],
                "outermost_frame_bbox": None,
                "innermost_frame_bbox": None,
                "frame_chain_indices": set(),
                "innermost_frame_indices": set(),
                "fallback_to_drawing_bbox": True,
                "reason": "invalid_drawing_area",
            }

        diagonal = math.hypot(
            drawing_bbox["maxX"] - drawing_bbox["minX"],
            drawing_bbox["maxY"] - drawing_bbox["minY"],
        )
        tolerance = max(diagonal * self.DETACHED_RECTANGLE_CONTACT_TOLERANCE_RATIO, 1.0)
        frame_like = self._collect_frame_like_candidates(
            candidates,
            geometry_entities,
            drawing_bbox,
            tolerance,
        )

        if not frame_like:
            return drawing_bbox, {
                "candidate_count": len(candidates),
                "chain_count": 0,
                "selected_chain_length": 0,
                "selected_chain_bboxes": [],
                "outermost_frame_bbox": None,
                "innermost_frame_bbox": None,
                "frame_chain_indices": set(),
                "innermost_frame_indices": set(),
                "fallback_to_drawing_bbox": True,
                "reason": "no_frame_like_candidates",
            }

        chains = self._build_concentric_frame_chains(frame_like, drawing_bbox, tolerance)
        selected_chain = self._select_valid_frame_chain(chains, drawing_bbox, geometry_entities, tolerance)
        if not selected_chain:
            return drawing_bbox, {
                "candidate_count": len(candidates),
                "chain_count": len(chains),
                "selected_chain_length": 0,
                "selected_chain_bboxes": [],
                "outermost_frame_bbox": None,
                "innermost_frame_bbox": None,
                "frame_chain_indices": set(),
                "innermost_frame_indices": set(),
                "fallback_to_drawing_bbox": True,
                "reason": "no_valid_chain",
            }

        chain_indices: Set[int] = set()
        chain_bboxes = [candidate["bbox"] for candidate in selected_chain]
        for candidate in selected_chain:
            chain_indices.update(candidate["indices"])

        innermost = selected_chain[-1]
        enclosing_frame_indices = self._collect_covering_frame_indices(
            candidates,
            innermost["bbox"],
            drawing_bbox,
        )
        innermost_interior_count = self._count_entities_inside_bbox(
            geometry_entities,
            innermost["bbox"],
            exclude_indices=chain_indices | enclosing_frame_indices,
        )
        innermost_edge_contact_count = self._count_entities_touching_bbox_boundary(
            geometry_entities,
            innermost["bbox"],
            exclude_indices=chain_indices | enclosing_frame_indices,
            tolerance=tolerance,
        )
        return innermost["bbox"], {
            "candidate_count": len(candidates),
            "chain_count": len(chains),
            "selected_chain_length": len(selected_chain),
            "selected_chain_bboxes": chain_bboxes,
            "outermost_frame_bbox": selected_chain[0]["bbox"],
            "innermost_frame_bbox": innermost["bbox"],
            "chain_bboxes": chain_bboxes,
            "frame_chain_indices": chain_indices,
            "innermost_frame_indices": set(innermost["indices"]),
            "innermost_interior_count": innermost_interior_count,
            "innermost_edge_contact_count": innermost_edge_contact_count,
            "fallback_to_drawing_bbox": False,
            "reason": "selected_valid_chain",
        }

    def _collect_covering_frame_indices(
        self,
        candidates: List[Dict[str, Any]],
        target_bbox: Dict[str, float],
        drawing_bbox: Dict[str, float],
    ) -> Set[int]:
        covering_indices: Set[int] = set()
        target_area = self._bbox_area(target_bbox)
        if target_area <= 0:
            return covering_indices

        for candidate in candidates:
            if candidate["area"] <= target_area:
                continue
            if not self._bbox_contains_bbox(candidate["bbox"], target_bbox, margin=1.0):
                continue
            if not self._has_similar_center(candidate["bbox"], target_bbox, drawing_bbox):
                continue
            covering_indices.update(candidate["indices"])

        return covering_indices

    def _collect_outer_wrapper_frames(
        self,
        geometry_entities: List[Tuple[int, FlattenedEntity]],
        work_area_bbox: Dict[str, float],
        frame_window_meta: Dict[str, Any],
        drawing_bbox: Dict[str, float],
    ) -> Tuple[Set[int], List[Dict[str, float]]]:
        if frame_window_meta.get("fallback_to_drawing_bbox"):
            return set(), []

        known_indices = set(frame_window_meta.get("frame_chain_indices", set()))
        rectangle_candidates = self._collect_frame_rectangle_candidates(geometry_entities)
        reference_bbox = self._frame_reference_bbox(rectangle_candidates, drawing_bbox)
        candidates = self._collect_frame_like_candidates(
            rectangle_candidates,
            geometry_entities,
            drawing_bbox,
        )
        drawing_area = self._bbox_area(drawing_bbox)
        wrappers = []
        for candidate in candidates:
            if candidate["indices"] & known_indices:
                continue
            if not self._bbox_contains_bbox(candidate["bbox"], work_area_bbox, margin=1.0):
                continue
            area_ratio = candidate["area"] / drawing_area if drawing_area > 0 else 0.0
            if area_ratio < self.FRAME_OUTER_WRAPPER_MIN_AREA_RATIO:
                continue
            if not self._is_outer_wrapper_bbox(candidate["bbox"], reference_bbox):
                continue
            wrappers.append(candidate)

        wrappers.sort(key=lambda item: item["area"], reverse=True)
        wrapper_indices: Set[int] = set()
        wrapper_bboxes: List[Dict[str, float]] = []
        for candidate in wrappers:
            wrapper_indices.update(candidate["indices"])
            wrapper_bboxes.append(candidate["bbox"])

        return wrapper_indices, wrapper_bboxes

    def _collect_enclosing_work_area_frames(
        self,
        geometry_entities: List[Tuple[int, FlattenedEntity]],
        work_area_bbox: Dict[str, float],
        frame_window_meta: Dict[str, Any],
        drawing_bbox: Dict[str, float],
    ) -> Tuple[Set[int], List[Dict[str, float]]]:
        if frame_window_meta.get("fallback_to_drawing_bbox"):
            return set(), []

        known_indices = set(frame_window_meta.get("frame_chain_indices", set()))
        work_area = self._bbox_area(work_area_bbox)
        if work_area <= 0:
            return set(), []

        enclosing_candidates = []
        for candidate in self._collect_frame_rectangle_candidates(geometry_entities):
            if candidate["indices"] & known_indices:
                continue
            if candidate["area"] <= work_area:
                continue
            if not self._bbox_contains_bbox(candidate["bbox"], work_area_bbox, margin=1.0):
                continue
            if not self._has_similar_center(candidate["bbox"], work_area_bbox, drawing_bbox):
                continue
            enclosing_candidates.append(candidate)

        enclosing_candidates.sort(key=lambda item: item["area"], reverse=True)
        enclosing_indices: Set[int] = set()
        enclosing_bboxes: List[Dict[str, float]] = []
        for candidate in enclosing_candidates:
            enclosing_indices.update(candidate["indices"])
            enclosing_bboxes.append(candidate["bbox"])

        return enclosing_indices, enclosing_bboxes

    def _collect_frame_like_candidates(
        self,
        candidates: List[Dict[str, Any]],
        geometry_entities: List[Tuple[int, FlattenedEntity]],
        drawing_bbox: Dict[str, float],
        tolerance: float | None = None,
    ) -> List[Dict[str, Any]]:
        drawing_area = self._bbox_area(drawing_bbox)
        if drawing_area <= 0:
            return []

        if tolerance is None:
            diagonal = math.hypot(
                drawing_bbox["maxX"] - drawing_bbox["minX"],
                drawing_bbox["maxY"] - drawing_bbox["minY"],
            )
            tolerance = max(diagonal * self.DETACHED_RECTANGLE_CONTACT_TOLERANCE_RATIO, 1.0)
        reference_bbox = self._frame_reference_bbox(candidates, drawing_bbox)

        entity_bboxes = {
            index: self._entity_bbox_dict(flattened)
            for index, flattened in geometry_entities
        }
        line_cache: Dict[int, List[LineString]] = {}
        frame_like = []
        for candidate in candidates:
            area_ratio = candidate["area"] / drawing_area
            if area_ratio < self.FRAME_CANDIDATE_MIN_AREA_RATIO:
                continue
            contact_count = self._count_rectangle_boundary_contacts_excluding_candidates(
                candidate_indices=candidate["indices"],
                candidate_bbox=candidate["bbox"],
                geometry_entities=geometry_entities,
                entity_bboxes=entity_bboxes,
                line_cache=line_cache,
                tolerance=tolerance,
            )
            allowed_contacts = self.FRAME_ALLOWED_STRAY_CONTACTS
            if contact_count > allowed_contacts:
                continue
            frame_like.append(candidate)
        return frame_like

    def _build_concentric_frame_chains(
        self,
        candidates: List[Dict[str, Any]],
        drawing_bbox: Dict[str, float],
        tolerance: float,
    ) -> List[List[Dict[str, Any]]]:
        chains: List[List[Dict[str, Any]]] = []
        sorted_candidates = sorted(candidates, key=lambda item: item["area"])

        for candidate in sorted_candidates:
            placed = False
            for chain in chains:
                outermost = chain[0]
                innermost = chain[-1]
                if (
                    self._bbox_contains_bbox(candidate["bbox"], outermost["bbox"], margin=tolerance)
                    and self._is_concentric_frame_bbox(outermost["bbox"], candidate["bbox"], drawing_bbox)
                ):
                    chain.insert(0, candidate)
                    placed = True
                    break
                if (
                    self._bbox_contains_bbox(innermost["bbox"], candidate["bbox"], margin=tolerance)
                    and self._is_concentric_frame_bbox(candidate["bbox"], innermost["bbox"], drawing_bbox)
                ):
                    chain.append(candidate)
                    placed = True
                    break

            if not placed:
                chains.append([candidate])

        for chain in chains:
            chain.sort(key=lambda item: item["area"], reverse=True)
        chains.sort(key=lambda chain: (len(chain), chain[0]["area"]), reverse=True)
        return chains

    def _select_valid_frame_chain(
        self,
        chains: List[List[Dict[str, Any]]],
        drawing_bbox: Dict[str, float],
        geometry_entities: List[Tuple[int, FlattenedEntity]],
        tolerance: float,
    ) -> List[Dict[str, Any]] | None:
        if not chains:
            return None

        drawing_center = (
            (drawing_bbox["minX"] + drawing_bbox["maxX"]) / 2.0,
            (drawing_bbox["minY"] + drawing_bbox["maxY"]) / 2.0,
        )
        drawing_area = self._bbox_area(drawing_bbox)
        min_interior_entities = max(
            self.FRAME_MIN_INTERIOR_ENTITY_COUNT,
            int(len(geometry_entities) * self.FRAME_INTERIOR_ENTITY_RATIO),
        )
        valid_chains = []

        for chain in chains:
            outermost = chain[0]["bbox"]
            innermost = chain[-1]["bbox"]
            if not self._bbox_contains_point(outermost, drawing_center, margin=tolerance):
                continue
            if self._bbox_area(innermost) < drawing_area * self.FRAME_INNER_MIN_AREA_RATIO:
                continue

            interior_count = self._count_entities_inside_bbox(
                geometry_entities,
                innermost,
                exclude_indices=set().union(*(candidate["indices"] for candidate in chain)),
            )
            if interior_count < min_interior_entities:
                continue

            valid_chains.append((chain, interior_count))

        if not valid_chains:
            return None

        valid_chains.sort(
            key=lambda item: (
                self._bbox_area(item[0][-1]["bbox"]),
                -len(item[0]),
            ),
        )
        selected_chain, _ = valid_chains[0]
        return selected_chain

    def _collect_frame_rectangle_candidates(
        self,
        geometry_entities: List[Tuple[int, FlattenedEntity]],
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        seen_keys: Set[Tuple[int, int, int, int]] = set()

        for index, flattened in geometry_entities:
            if not self.parser._is_closed_rectangle(flattened):
                continue
            bbox = self._entity_bbox_dict(flattened)
            if bbox is None:
                continue
            key = self._bbox_key(bbox)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            candidates.append({
                "indices": {index},
                "bbox": bbox,
                "area": self._bbox_area(bbox),
                "source": "polyline",
            })

        for candidate in self._collect_4line_rectangle_candidates(geometry_entities):
            key = self._bbox_key(candidate["bbox"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            candidates.append(candidate)

        return candidates

    def _collect_4line_rectangle_candidates(
        self,
        geometry_entities: List[Tuple[int, FlattenedEntity]],
    ) -> List[Dict[str, Any]]:
        if len(geometry_entities) < 4:
            return []

        horizontal_lines: List[Dict[str, float | int]] = []
        vertical_lines: List[Dict[str, float | int]] = []
        overall_bbox = self._calculate_flattened_bbox([flattened for _, flattened in geometry_entities])
        diagonal = math.hypot(
            overall_bbox["maxX"] - overall_bbox["minX"],
            overall_bbox["maxY"] - overall_bbox["minY"],
        )
        tolerance = max(diagonal * 0.0015, 1.0)

        for index, flattened in geometry_entities:
            if flattened.entity.dxftype() != "LINE":
                continue
            start = self.parser._apply_transform(
                (flattened.entity.dxf.start.x, flattened.entity.dxf.start.y),
                flattened.transform,
            )
            end = self.parser._apply_transform(
                (flattened.entity.dxf.end.x, flattened.entity.dxf.end.y),
                flattened.transform,
            )
            dx = abs(end.x - start.x)
            dy = abs(end.y - start.y)
            if dx <= tolerance and dy > tolerance:
                y1, y2 = sorted((start.y, end.y))
                vertical_lines.append({
                    "index": index,
                    "x": start.x,
                    "y1": y1,
                    "y2": y2,
                })
            elif dy <= tolerance and dx > tolerance:
                x1, x2 = sorted((start.x, end.x))
                horizontal_lines.append({
                    "index": index,
                    "y": start.y,
                    "x1": x1,
                    "x2": x2,
                })

        horizontal_by_span: Dict[Tuple[int, int], List[Tuple[int, float, float]]] = {}
        vertical_by_span: Dict[Tuple[int, int], List[Tuple[int, float, float]]] = {}

        for line in horizontal_lines:
            span_key = (round(float(line["x1"]) / tolerance), round(float(line["x2"]) / tolerance))
            horizontal_by_span.setdefault(span_key, []).append(
                (int(line["index"]), float(line["y"]), float(line["x1"]), float(line["x2"]))
            )

        for line in vertical_lines:
            span_key = (round(float(line["y1"]) / tolerance), round(float(line["y2"]) / tolerance))
            vertical_by_span.setdefault(span_key, []).append(
                (int(line["index"]), float(line["x"]), float(line["y1"]), float(line["y2"]))
            )

        candidates: List[Dict[str, Any]] = []
        for _, horizontals in horizontal_by_span.items():
            if len(horizontals) < 2:
                continue
            sorted_h = sorted(horizontals, key=lambda item: item[1])
            for left in range(len(sorted_h)):
                for right in range(left + 1, len(sorted_h)):
                    low = sorted_h[left]
                    high = sorted_h[right]
                    x1 = low[2]
                    x2 = low[3]
                    if abs(high[2] - x1) > tolerance or abs(high[3] - x2) > tolerance:
                        continue
                    y1 = low[1]
                    y2 = high[1]
                    if y2 - y1 <= tolerance:
                        continue
                    v_span_key = (round(y1 / tolerance), round(y2 / tolerance))
                    verticals = vertical_by_span.get(v_span_key, [])
                    if len(verticals) < 2:
                        continue

                    left_vertical = None
                    right_vertical = None
                    for vertical in verticals:
                        x = vertical[1]
                        if left_vertical is None and abs(x - x1) <= tolerance:
                            left_vertical = vertical
                        elif right_vertical is None and abs(x - x2) <= tolerance:
                            right_vertical = vertical
                    if left_vertical is None or right_vertical is None:
                        continue

                    bbox = {
                        "minX": min(x1, x2),
                        "minY": y1,
                        "maxX": max(x1, x2),
                        "maxY": y2,
                    }
                    candidates.append({
                        "indices": {low[0], high[0], left_vertical[0], right_vertical[0]},
                        "bbox": bbox,
                        "area": self._bbox_area(bbox),
                        "source": "4line",
                    })

        return candidates

    def _count_rectangle_boundary_contacts_excluding_candidates(
        self,
        candidate_indices: Set[int],
        candidate_bbox: Dict[str, float],
        geometry_entities: List[Tuple[int, FlattenedEntity]],
        entity_bboxes: Dict[int, Dict[str, float] | None],
        line_cache: Dict[int, List[LineString]],
        tolerance: float,
    ) -> int:
        boundary = self._rectangle_boundary_lines(candidate_bbox)
        expanded_bbox = {
            "minX": candidate_bbox["minX"] - tolerance,
            "minY": candidate_bbox["minY"] - tolerance,
            "maxX": candidate_bbox["maxX"] + tolerance,
            "maxY": candidate_bbox["maxY"] + tolerance,
        }
        contact_indices: Set[int] = set()

        for other_index, other_flattened in geometry_entities:
            if other_index in candidate_indices:
                continue

            other_bbox = entity_bboxes.get(other_index)
            if other_bbox is None or not self._bbox_intersects_bbox(expanded_bbox, other_bbox):
                continue

            for line in self._geometry_line_strings(other_index, other_flattened, line_cache):
                if line.is_empty:
                    continue
                buffered = line.buffer(tolerance, cap_style="flat")
                if any(buffered.intersects(edge) for edge in boundary):
                    contact_indices.add(other_index)
                    break

        return len(contact_indices)

    def _count_entities_touching_bbox_boundary(
        self,
        geometry_entities: List[Tuple[int, FlattenedEntity]],
        bbox: Dict[str, float],
        exclude_indices: Set[int],
        tolerance: float,
    ) -> int:
        entity_bboxes = {
            index: self._entity_bbox_dict(flattened)
            for index, flattened in geometry_entities
        }
        line_cache: Dict[int, List[LineString]] = {}
        return self._count_rectangle_boundary_contacts_excluding_candidates(
            candidate_indices=exclude_indices,
            candidate_bbox=bbox,
            geometry_entities=geometry_entities,
            entity_bboxes=entity_bboxes,
            line_cache=line_cache,
            tolerance=tolerance,
        )

    def _is_frame_seed_bbox(
        self,
        bbox: Dict[str, float],
        drawing_bbox: Dict[str, float],
    ) -> bool:
        return self._is_concentric_frame_bbox(bbox, drawing_bbox, drawing_bbox)

    def _is_concentric_frame_bbox(
        self,
        bbox: Dict[str, float],
        reference_bbox: Dict[str, float],
        drawing_bbox: Dict[str, float],
    ) -> bool:
        tolerance = max(
            math.hypot(
                drawing_bbox["maxX"] - drawing_bbox["minX"],
                drawing_bbox["maxY"] - drawing_bbox["minY"],
            ) * self.FRAME_INSET_TOLERANCE_RATIO,
            2.0,
        )
        if not self._has_similar_center(bbox, reference_bbox, drawing_bbox):
            return False
        if not self._has_similar_aspect_ratio(bbox, reference_bbox):
            return False

        left_gap = bbox["minX"] - reference_bbox["minX"]
        right_gap = reference_bbox["maxX"] - bbox["maxX"]
        bottom_gap = bbox["minY"] - reference_bbox["minY"]
        top_gap = reference_bbox["maxY"] - bbox["maxY"]
        gaps = [left_gap, right_gap, bottom_gap, top_gap]
        if any(gap < -tolerance for gap in gaps):
            return False
        return max(gaps) - min(gaps) <= tolerance * 2.5

    def _is_outer_wrapper_bbox(
        self,
        bbox: Dict[str, float],
        reference_bbox: Dict[str, float],
    ) -> bool:
        return (
            self._is_concentric_frame_bbox(bbox, reference_bbox, reference_bbox)
            or self._bbox_edge_anchor_count(bbox, reference_bbox) >= 3
        )

    def _frame_reference_bbox(
        self,
        candidates: List[Dict[str, Any]],
        fallback_bbox: Dict[str, float],
    ) -> Dict[str, float]:
        if not candidates:
            return fallback_bbox

        return {
            "minX": min(candidate["bbox"]["minX"] for candidate in candidates),
            "minY": min(candidate["bbox"]["minY"] for candidate in candidates),
            "maxX": max(candidate["bbox"]["maxX"] for candidate in candidates),
            "maxY": max(candidate["bbox"]["maxY"] for candidate in candidates),
        }

    def _has_similar_center(
        self,
        bbox: Dict[str, float],
        reference_bbox: Dict[str, float],
        drawing_bbox: Dict[str, float],
    ) -> bool:
        center = ((bbox["minX"] + bbox["maxX"]) / 2.0, (bbox["minY"] + bbox["maxY"]) / 2.0)
        reference_center = (
            (reference_bbox["minX"] + reference_bbox["maxX"]) / 2.0,
            (reference_bbox["minY"] + reference_bbox["maxY"]) / 2.0,
        )
        diagonal = math.hypot(
            drawing_bbox["maxX"] - drawing_bbox["minX"],
            drawing_bbox["maxY"] - drawing_bbox["minY"],
        )
        tolerance = max(diagonal * self.FRAME_CENTER_TOLERANCE_RATIO, 5.0)
        return math.hypot(center[0] - reference_center[0], center[1] - reference_center[1]) <= tolerance

    def _has_similar_aspect_ratio(
        self,
        bbox: Dict[str, float],
        reference_bbox: Dict[str, float],
    ) -> bool:
        width = max(bbox["maxX"] - bbox["minX"], 1e-6)
        height = max(bbox["maxY"] - bbox["minY"], 1e-6)
        ref_width = max(reference_bbox["maxX"] - reference_bbox["minX"], 1e-6)
        ref_height = max(reference_bbox["maxY"] - reference_bbox["minY"], 1e-6)
        return abs((width / height) - (ref_width / ref_height)) <= self.FRAME_ASPECT_TOLERANCE

    def _should_keep_entity_in_work_area(
        self,
        flattened: FlattenedEntity,
        work_area_bbox: Dict[str, float],
    ) -> bool:
        entity_bbox = self._entity_bbox_dict(flattened)
        if entity_bbox is None:
            center = self._flattened_entity_center(flattened)
            return center is not None and self._bbox_contains_point(work_area_bbox, center)

        center = (
            (entity_bbox["minX"] + entity_bbox["maxX"]) / 2.0,
            (entity_bbox["minY"] + entity_bbox["maxY"]) / 2.0,
        )
        if self._bbox_contains_point(work_area_bbox, center):
            return True

        entity_area = self._bbox_area(entity_bbox)
        if entity_area <= 1e-6:
            return self._bbox_intersects_bbox(entity_bbox, work_area_bbox)

        overlap = self._bbox_intersection_area(entity_bbox, work_area_bbox)
        return (overlap / entity_area) > self.FRAME_KEEP_OVERLAP_RATIO

    def _count_entities_inside_bbox(
        self,
        geometry_entities: List[Tuple[int, FlattenedEntity]],
        bbox: Dict[str, float],
        exclude_indices: Set[int],
    ) -> int:
        count = 0
        for index, flattened in geometry_entities:
            if index in exclude_indices:
                continue
            if self._should_keep_entity_in_work_area(flattened, bbox):
                count += 1
        return count

    def _bbox_key(self, bbox: Dict[str, float], precision: float = 1.0) -> Tuple[int, int, int, int]:
        return (
            round(bbox["minX"] / precision),
            round(bbox["minY"] / precision),
            round(bbox["maxX"] / precision),
            round(bbox["maxY"] / precision),
        )

    def _detect_detached_rectangle_indices(
        self,
        geometry_entities: List[Tuple[int, FlattenedEntity]],
        drawing_bbox: Dict[str, float],
    ) -> Set[int]:
        if len(geometry_entities) < 2:
            return set()

        drawing_area = self._bbox_area(drawing_bbox)
        if drawing_area <= 0:
            return set()

        diagonal = math.hypot(
            drawing_bbox["maxX"] - drawing_bbox["minX"],
            drawing_bbox["maxY"] - drawing_bbox["minY"],
        )
        contact_tolerance = max(diagonal * self.DETACHED_RECTANGLE_CONTACT_TOLERANCE_RATIO, 1.0)
        entity_bboxes = {
            index: self._entity_bbox_dict(flattened)
            for index, flattened in geometry_entities
        }
        line_cache: Dict[int, List[LineString]] = {}
        detached: Set[int] = set()

        for index, flattened in geometry_entities:
            if not self.parser._is_closed_rectangle(flattened):
                continue

            bbox = entity_bboxes.get(index)
            if bbox is None:
                continue

            area = self._bbox_area(bbox)
            if area <= 0 or area >= drawing_area * self.DETACHED_RECTANGLE_MAX_AREA_RATIO:
                continue
            if self._bbox_touches_corner(bbox, drawing_bbox):
                continue

            if self._rectangle_has_boundary_contacts(
                candidate_index=index,
                candidate_bbox=bbox,
                geometry_entities=geometry_entities,
                entity_bboxes=entity_bboxes,
                line_cache=line_cache,
                tolerance=contact_tolerance,
            ):
                continue

            detached.add(index)

        return detached

    def _rectangle_has_boundary_contacts(
        self,
        candidate_index: int,
        candidate_bbox: Dict[str, float],
        geometry_entities: List[Tuple[int, FlattenedEntity]],
        entity_bboxes: Dict[int, Dict[str, float] | None],
        line_cache: Dict[int, List[LineString]],
        tolerance: float,
    ) -> bool:
        boundary = self._rectangle_boundary_lines(candidate_bbox)
        expanded_bbox = {
            "minX": candidate_bbox["minX"] - tolerance,
            "minY": candidate_bbox["minY"] - tolerance,
            "maxX": candidate_bbox["maxX"] + tolerance,
            "maxY": candidate_bbox["maxY"] + tolerance,
        }

        for other_index, other_flattened in geometry_entities:
            if other_index == candidate_index:
                continue

            other_bbox = entity_bboxes.get(other_index)
            if other_bbox is None or not self._bbox_intersects_bbox(expanded_bbox, other_bbox):
                continue

            for line in self._geometry_line_strings(other_index, other_flattened, line_cache):
                if line.is_empty:
                    continue
                buffered = line.buffer(tolerance, cap_style="flat")
                if any(buffered.intersects(edge) for edge in boundary):
                    return True

        return False

    def _geometry_line_strings(
        self,
        index: int,
        flattened: FlattenedEntity,
        line_cache: Dict[int, List[LineString]],
    ) -> List[LineString]:
        cached = line_cache.get(index)
        if cached is not None:
            return cached

        lines = []
        for segment in self.parser._process_flattened_entity(flattened):
            start = segment.start.to_2d()
            end = segment.end.to_2d()
            if start == end:
                continue
            lines.append(LineString([start, end]))

        line_cache[index] = lines
        return lines

    def _rectangle_boundary_lines(self, bbox: Dict[str, float]) -> List[LineString]:
        min_x = bbox["minX"]
        min_y = bbox["minY"]
        max_x = bbox["maxX"]
        max_y = bbox["maxY"]
        corners = [
            (min_x, min_y),
            (max_x, min_y),
            (max_x, max_y),
            (min_x, max_y),
        ]
        return [
            LineString([corners[0], corners[1]]),
            LineString([corners[1], corners[2]]),
            LineString([corners[2], corners[3]]),
            LineString([corners[3], corners[0]]),
        ]

    def _should_remove_annotation_geometry(
        self,
        flattened: FlattenedEntity,
        drawing_bbox: Dict[str, float],
        layer_type_stats: Dict[str, Dict[str, int]],
        short_line_threshold: float,
        annotation_context_centers: List[Tuple[float, float]],
        annotation_signal_centers: List[Tuple[float, float]],
    ) -> bool:
        if flattened.entity.dxftype() not in self.parser.GEOMETRY_ENTITY_TYPES:
            return False

        if self._is_protected_boundary_layer(flattened.effective_layer):
            return False

        shape_evidence = self._has_annotation_shape_evidence(flattened)
        if not shape_evidence:
            return False

        context_evidence = self._has_annotation_context_evidence(flattened, layer_type_stats)
        if not context_evidence:
            return False

        position_evidence = self._has_annotation_position_evidence(
            flattened,
            drawing_bbox,
            short_line_threshold,
            annotation_context_centers,
            annotation_signal_centers,
        )
        return position_evidence

    def _has_annotation_shape_evidence(self, flattened: FlattenedEntity) -> bool:
        entity = flattened.entity
        if self.parser._looks_like_breakline_polyline(entity, transform=flattened.transform):
            return True
        return self.parser._is_open_annotation_geometry(entity)

    def _has_annotation_context_evidence(
        self,
        flattened: FlattenedEntity,
        layer_type_stats: Dict[str, Dict[str, int]],
    ) -> bool:
        layer_name = str(flattened.effective_layer or "")
        linetype_name = str(flattened.effective_linetype or "")

        if any(self.parser._matches_breakline_name(name) for name in (layer_name, *flattened.block_path)):
            return True

        if flattened.block_path:
            for block_name in flattened.block_path:
                if self.parser._matches_dimension_block_name(block_name):
                    return True
                if self.parser._matches_annotation_name(block_name):
                    return True
                if self._matches_keyword_group(block_name, self.ANNOTATION_BLOCK_KEYWORDS):
                    return True

        if self.parser._matches_annotation_name(layer_name):
            return True
        if self.parser._matches_annotation_linetype_name(linetype_name):
            return True
        if self._matches_keyword_group(layer_name, self.ANNOTATION_LAYER_KEYWORDS):
            return True
        if self._matches_keyword_group(linetype_name, tuple(keyword.lower() for keyword in self.ANNOTATION_LINETYPE_KEYWORDS)):
            return True

        return self._layer_annotation_ratio(layer_name, layer_type_stats) >= self.ANNOTATION_LAYER_RATIO_THRESHOLD

    def _has_annotation_position_evidence(
        self,
        flattened: FlattenedEntity,
        drawing_bbox: Dict[str, float],
        short_line_threshold: float,
        annotation_context_centers: List[Tuple[float, float]],
        annotation_signal_centers: List[Tuple[float, float]],
    ) -> bool:
        entity_bbox = self._entity_bbox_dict(flattened)
        if entity_bbox is not None and self._bbox_touches_drawing_edge(entity_bbox, drawing_bbox):
            return True

        center = self._flattened_entity_center(flattened)
        if center is None:
            return False

        diagonal = math.hypot(
            drawing_bbox["maxX"] - drawing_bbox["minX"],
            drawing_bbox["maxY"] - drawing_bbox["minY"],
        )
        radius = max(short_line_threshold * 4.0, diagonal * self.ANNOTATION_CONTEXT_RADIUS_RATIO, 25.0)

        nearby_signal_count = self._count_nearby_points(center, annotation_signal_centers, radius)
        if nearby_signal_count >= 1:
            return True

        nearby_context_count = self._count_nearby_points(center, annotation_context_centers, radius, exclude_point=center)
        return nearby_context_count >= 1

    def _build_annotation_context_centers(
        self,
        flattened_entities: List[FlattenedEntity],
        layer_type_stats: Dict[str, Dict[str, int]],
    ) -> List[Tuple[float, float]]:
        centers: List[Tuple[float, float]] = []
        for flattened in flattened_entities:
            if flattened.entity.dxftype() in self.parser.FORCED_DELETE_TYPES:
                center = self._flattened_entity_center(flattened)
                if center is not None:
                    centers.append(center)
                continue

            if not self._has_annotation_context_evidence(flattened, layer_type_stats):
                continue

            center = self._flattened_entity_center(flattened)
            if center is not None:
                centers.append(center)
        return centers

    def _build_annotation_signal_centers(
        self,
        flattened_entities: List[FlattenedEntity],
    ) -> List[Tuple[float, float]]:
        centers: List[Tuple[float, float]] = []
        for flattened in flattened_entities:
            if flattened.entity.dxftype() not in self.parser.TITLE_SIGNAL_TYPES:
                continue
            center = self._flattened_entity_center(flattened)
            if center is not None:
                centers.append(center)
        return centers

    def _count_nearby_points(
        self,
        point: Tuple[float, float],
        candidates: List[Tuple[float, float]],
        radius: float,
        exclude_point: Tuple[float, float] | None = None,
    ) -> int:
        count = 0
        for candidate in candidates:
            if exclude_point is not None and self._points_close(candidate, exclude_point):
                continue
            if math.hypot(point[0] - candidate[0], point[1] - candidate[1]) <= radius:
                count += 1
        return count

    def _points_close(
        self,
        left: Tuple[float, float],
        right: Tuple[float, float],
        tolerance: float = 1e-6,
    ) -> bool:
        return abs(left[0] - right[0]) <= tolerance and abs(left[1] - right[1]) <= tolerance

    def _detect_title_block_candidate_bbox(
        self,
        flattened_entities: List[FlattenedEntity],
        drawing_bbox: Dict[str, float],
        excluded_ids: Set[int],
    ) -> Dict[str, float] | None:
        width = drawing_bbox["maxX"] - drawing_bbox["minX"]
        height = drawing_bbox["maxY"] - drawing_bbox["minY"]
        if width <= 0 or height <= 0:
            return None

        grid_size = self.TITLE_BLOCK_GRID_SIZE
        cell_width = width / grid_size
        cell_height = height / grid_size
        if cell_width <= 0 or cell_height <= 0:
            return None

        diagonal = math.hypot(width, height)
        short_line_threshold = self._estimate_short_line_threshold(
            flattened_entities,
            drawing_bbox,
            excluded_ids,
        )
        structured_line_threshold = max(short_line_threshold * 3.0, diagonal * 0.12)

        density = [[0.0 for _ in range(grid_size)] for _ in range(grid_size)]
        for index, flattened in enumerate(flattened_entities):
            if index in excluded_ids:
                continue

            center = self._flattened_entity_center(flattened)
            if center is None:
                continue

            col = min(grid_size - 1, max(0, int((center[0] - drawing_bbox["minX"]) / cell_width)))
            row = min(grid_size - 1, max(0, int((center[1] - drawing_bbox["minY"]) / cell_height)))
            dxftype = flattened.entity.dxftype()

            if dxftype in self.parser.TITLE_SIGNAL_TYPES:
                density[row][col] += 2.5
                continue

            if dxftype == "LINE":
                length = self._flattened_line_length(flattened)
                if length <= short_line_threshold:
                    density[row][col] += 1.0
                elif length <= structured_line_threshold:
                    density[row][col] += 0.8

        non_zero_scores = [score for row in density for score in row if score > 0]
        if non_zero_scores:
            threshold = max(4.5, float(np.percentile(non_zero_scores, 82)))
            visited = set()
            best_cluster = None
            best_score = 0.0

            for row in range(grid_size):
                for col in range(grid_size):
                    if (row, col) in visited or density[row][col] < threshold:
                        continue
                    cluster, score = self._collect_density_cluster(density, row, col, threshold, visited)
                    if not cluster:
                        continue
                    if not any(self._cell_is_edge(r, c, grid_size) for r, c in cluster):
                        continue
                    if not self._cluster_is_corner_localized(cluster, grid_size):
                        continue
                    if score > best_score:
                        best_cluster = cluster
                        best_score = score

            if best_cluster is not None and best_score >= 6.0:
                rows = [row for row, _ in best_cluster]
                cols = [col for _, col in best_cluster]
                return {
                    "minX": drawing_bbox["minX"] + min(cols) * cell_width,
                    "minY": drawing_bbox["minY"] + min(rows) * cell_height,
                    "maxX": drawing_bbox["minX"] + (max(cols) + 1) * cell_width,
                    "maxY": drawing_bbox["minY"] + (max(rows) + 1) * cell_height,
                }

        return self._detect_title_block_by_rectangle(flattened_entities, drawing_bbox, excluded_ids)

    def _confirm_title_block_bbox(
        self,
        flattened_entities: List[FlattenedEntity],
        drawing_bbox: Dict[str, float],
        candidate_bbox: Dict[str, float] | None,
        excluded_ids: Set[int],
        layer_type_stats: Dict[str, Dict[str, int]],
        structured_line_threshold: float,
    ) -> Tuple[Dict[str, float] | None, Dict[str, Any]]:
        debug = {
            "candidate_bbox": candidate_bbox,
            "confirmed": False,
            "signal_count": 0,
            "short_line_count": 0,
            "contained_entity_count": 0,
            "title_like_entity_count": 0,
            "crossing_long_geometry_count": 0,
            "reasons": [],
        }
        if candidate_bbox is None:
            debug["reasons"].append("no_candidate")
            return None, debug

        drawing_area = self._bbox_area(drawing_bbox)
        candidate_area = self._bbox_area(candidate_bbox)
        if drawing_area <= 0 or candidate_area <= 0:
            debug["reasons"].append("invalid_bbox_area")
            return None, debug

        drawing_width = drawing_bbox["maxX"] - drawing_bbox["minX"]
        drawing_height = drawing_bbox["maxY"] - drawing_bbox["minY"]
        candidate_width = candidate_bbox["maxX"] - candidate_bbox["minX"]
        candidate_height = candidate_bbox["maxY"] - candidate_bbox["minY"]
        width_ratio = candidate_width / drawing_width if drawing_width > 0 else 1.0
        height_ratio = candidate_height / drawing_height if drawing_height > 0 else 1.0
        area_ratio = candidate_area / drawing_area if drawing_area > 0 else 1.0
        debug["width_ratio"] = width_ratio
        debug["height_ratio"] = height_ratio
        debug["area_ratio"] = area_ratio

        if area_ratio > self.TITLE_BLOCK_MAX_AREA_RATIO:
            debug["reasons"].append("candidate_too_large")
            return None, debug
        if width_ratio > self.TITLE_BLOCK_MAX_WIDTH_RATIO:
            debug["reasons"].append("candidate_too_wide")
            return None, debug
        if height_ratio > self.TITLE_BLOCK_MAX_HEIGHT_RATIO:
            debug["reasons"].append("candidate_too_tall")
            return None, debug
        if not self._bbox_touches_corner(candidate_bbox, drawing_bbox):
            debug["reasons"].append("not_corner_anchored")
            return None, debug

        contained_indices: List[int] = []
        title_like_indices: List[int] = []
        signal_count = 0
        short_line_count = 0
        long_crossing_geometry = 0
        diagonal = math.hypot(
            drawing_bbox["maxX"] - drawing_bbox["minX"],
            drawing_bbox["maxY"] - drawing_bbox["minY"],
        )

        for index, flattened in enumerate(flattened_entities):
            if index in excluded_ids:
                continue

            entity_bbox = self._entity_bbox_dict(flattened)
            if entity_bbox is None:
                continue

            contained = self._bbox_contains_bbox(candidate_bbox, entity_bbox, margin=1e-6)
            intersects = self._bbox_intersects_bbox(candidate_bbox, entity_bbox)

            if contained:
                contained_indices.append(index)
                dxftype = flattened.entity.dxftype()
                if dxftype in self.parser.TITLE_SIGNAL_TYPES:
                    signal_count += 1
                if self._is_title_block_helper_entity(
                    flattened,
                    candidate_bbox,
                    drawing_bbox,
                    layer_type_stats,
                    structured_line_threshold,
                ):
                    title_like_indices.append(index)
                if dxftype == "LINE" and self._is_title_block_table_line(
                    flattened,
                    candidate_bbox,
                    structured_line_threshold,
                ):
                    short_line_count += 1
            elif intersects and flattened.entity.dxftype() in self.parser.GEOMETRY_ENTITY_TYPES:
                entity_span = max(
                    entity_bbox["maxX"] - entity_bbox["minX"],
                    entity_bbox["maxY"] - entity_bbox["minY"],
                )
                if entity_span >= diagonal * 0.06:
                    long_crossing_geometry += 1

        debug["signal_count"] = signal_count
        debug["short_line_count"] = short_line_count
        debug["contained_entity_count"] = len(contained_indices)
        debug["title_like_entity_count"] = len(title_like_indices)
        debug["crossing_long_geometry_count"] = long_crossing_geometry

        if signal_count < 3:
            debug["reasons"].append("insufficient_text_signals")
            return None, debug

        if short_line_count < 2 and signal_count < 4:
            debug["reasons"].append("insufficient_table_structure")
            return None, debug

        if long_crossing_geometry > 0:
            debug["reasons"].append("crosses_large_geometry")
            return None, debug

        tight_bbox = self._tight_bbox_for_indices(flattened_entities, title_like_indices)
        if tight_bbox is None:
            debug["reasons"].append("failed_to_tighten_bbox")
            return None, debug

        debug["confirmed"] = True
        debug["tight_bbox"] = tight_bbox
        return tight_bbox, debug

    def _detect_title_block_by_rectangle(
        self,
        flattened_entities: List[FlattenedEntity],
        drawing_bbox: Dict[str, float],
        excluded_ids: Set[int],
    ) -> Dict[str, float] | None:
        rectangle_candidates: List[Tuple[float, Dict[str, float]]] = []
        drawing_area = self._bbox_area(drawing_bbox)
        if drawing_area <= 0:
            return None

        for index, flattened in enumerate(flattened_entities):
            if index in excluded_ids:
                continue
            if not self.parser._is_closed_rectangle(flattened):
                continue

            bbox = self._entity_bbox_dict(flattened)
            if bbox is None:
                continue

            rect_area = self._bbox_area(bbox)
            if rect_area <= 0 or rect_area >= drawing_area * 0.5:
                continue
            drawing_width = drawing_bbox["maxX"] - drawing_bbox["minX"]
            drawing_height = drawing_bbox["maxY"] - drawing_bbox["minY"]
            rect_width = bbox["maxX"] - bbox["minX"]
            rect_height = bbox["maxY"] - bbox["minY"]
            width_ratio = rect_width / drawing_width if drawing_width > 0 else 1.0
            height_ratio = rect_height / drawing_height if drawing_height > 0 else 1.0
            if width_ratio > self.TITLE_BLOCK_MAX_WIDTH_RATIO:
                continue
            if height_ratio > self.TITLE_BLOCK_MAX_HEIGHT_RATIO:
                continue
            if rect_area / drawing_area > self.TITLE_BLOCK_MAX_AREA_RATIO:
                continue
            if not self._bbox_touches_corner(bbox, drawing_bbox):
                continue
            rectangle_candidates.append((rect_area, bbox))

        rectangle_candidates.sort(reverse=True)
        structured_line_threshold = self._structured_line_threshold(
            drawing_bbox,
            self._estimate_short_line_threshold(flattened_entities, drawing_bbox, excluded_ids),
        )
        for _, bbox in rectangle_candidates[:3]:
            contained_signals = 0
            contained_short_lines = 0
            for index, flattened in enumerate(flattened_entities):
                if index in excluded_ids:
                    continue
                entity_bbox = self._entity_bbox_dict(flattened)
                if entity_bbox is None or not self._bbox_contains_bbox(bbox, entity_bbox, margin=1e-6):
                    continue

                dxftype = flattened.entity.dxftype()
                if dxftype in self.parser.TITLE_SIGNAL_TYPES:
                    contained_signals += 1
                elif dxftype == "LINE" and self._flattened_line_length(flattened) <= structured_line_threshold:
                    contained_short_lines += 1

            if contained_signals >= 3 and contained_short_lines >= 2:
                return bbox
        return None

    def _estimate_short_line_threshold(
        self,
        flattened_entities: List[FlattenedEntity],
        drawing_bbox: Dict[str, float],
        excluded_ids: Set[int],
    ) -> float:
        line_lengths = []
        for index, flattened in enumerate(flattened_entities):
            if index in excluded_ids or flattened.entity.dxftype() != "LINE":
                continue
            line_lengths.append(self._flattened_line_length(flattened))

        diagonal = math.hypot(
            drawing_bbox["maxX"] - drawing_bbox["minX"],
            drawing_bbox["maxY"] - drawing_bbox["minY"],
        )
        return min(
            float(np.percentile(line_lengths, 35)) if line_lengths else diagonal * 0.03,
            diagonal * 0.03,
        )

    def _structured_line_threshold(
        self,
        drawing_bbox: Dict[str, float],
        short_line_threshold: float,
    ) -> float:
        diagonal = math.hypot(
            drawing_bbox["maxX"] - drawing_bbox["minX"],
            drawing_bbox["maxY"] - drawing_bbox["minY"],
        )
        return max(short_line_threshold * 3.0, diagonal * 0.12)

    def _flattened_line_length(self, flattened: FlattenedEntity) -> float:
        start = self.parser._apply_transform(
            (flattened.entity.dxf.start.x, flattened.entity.dxf.start.y),
            flattened.transform,
        )
        end = self.parser._apply_transform(
            (flattened.entity.dxf.end.x, flattened.entity.dxf.end.y),
            flattened.transform,
        )
        return math.hypot(end.x - start.x, end.y - start.y)

    def _collect_density_cluster(
        self,
        density: List[List[float]],
        start_row: int,
        start_col: int,
        threshold: float,
        visited: Set[Tuple[int, int]],
    ) -> Tuple[Set[Tuple[int, int]], float]:
        cluster: Set[Tuple[int, int]] = set()
        total_score = 0.0
        queue = deque([(start_row, start_col)])
        visited.add((start_row, start_col))
        grid_size = len(density)

        while queue:
            row, col = queue.popleft()
            cluster.add((row, col))
            total_score += density[row][col]

            for next_row, next_col in ((row - 1, col), (row + 1, col), (row, col - 1), (row, col + 1)):
                if not (0 <= next_row < grid_size and 0 <= next_col < grid_size):
                    continue
                if (next_row, next_col) in visited or density[next_row][next_col] < threshold:
                    continue
                visited.add((next_row, next_col))
                queue.append((next_row, next_col))

        return cluster, total_score

    def _cell_is_edge(self, row: int, col: int, grid_size: int) -> bool:
        return row == 0 or col == 0 or row == grid_size - 1 or col == grid_size - 1

    def _cluster_is_corner_localized(
        self,
        cluster: Set[Tuple[int, int]],
        grid_size: int,
    ) -> bool:
        rows = [row for row, _ in cluster]
        cols = [col for _, col in cluster]
        min_row = min(rows)
        max_row = max(rows)
        min_col = min(cols)
        max_col = max(cols)

        anchored_top = min_row == 0 and max_row <= self.TITLE_BLOCK_MAX_CORNER_ROW_SPAN - 1
        anchored_bottom = max_row == grid_size - 1 and min_row >= grid_size - self.TITLE_BLOCK_MAX_CORNER_ROW_SPAN
        anchored_left = min_col == 0 and max_col <= self.TITLE_BLOCK_MAX_CORNER_COL_SPAN - 1
        anchored_right = max_col == grid_size - 1 and min_col >= grid_size - self.TITLE_BLOCK_MAX_CORNER_COL_SPAN

        return (
            (anchored_top and anchored_left)
            or (anchored_top and anchored_right)
            or (anchored_bottom and anchored_left)
            or (anchored_bottom and anchored_right)
        )

    def _tight_bbox_for_indices(
        self,
        flattened_entities: List[FlattenedEntity],
        indices: List[int],
    ) -> Dict[str, float] | None:
        bboxes = [
            self._entity_bbox_dict(flattened_entities[index])
            for index in indices
        ]
        valid = [bbox for bbox in bboxes if bbox is not None]
        if not valid:
            return None

        return {
            "minX": min(bbox["minX"] for bbox in valid),
            "minY": min(bbox["minY"] for bbox in valid),
            "maxX": max(bbox["maxX"] for bbox in valid),
            "maxY": max(bbox["maxY"] for bbox in valid),
        }

    def _build_layer_type_stats(
        self,
        flattened_entities: List[FlattenedEntity],
    ) -> Dict[str, Dict[str, int]]:
        layer_stats: Dict[str, Dict[str, int]] = {}
        for flattened in flattened_entities:
            layer_name = str(flattened.effective_layer or "")
            entity_type = flattened.entity.dxftype()
            counts = layer_stats.setdefault(layer_name, {})
            counts[entity_type] = counts.get(entity_type, 0) + 1
        return layer_stats

    def _layer_annotation_ratio(
        self,
        layer_name: str,
        layer_type_stats: Dict[str, Dict[str, int]],
    ) -> float:
        type_counts = layer_type_stats.get(layer_name) or {}
        total = sum(type_counts.values())
        if total <= 0:
            return 0.0
        annotation_count = sum(
            count
            for entity_type, count in type_counts.items()
            if entity_type in self.parser.TITLE_SIGNAL_TYPES
        )
        return annotation_count / total

    def _matches_keyword_group(self, value: str | None, keywords: Tuple[str, ...]) -> bool:
        if not value:
            return False
        lowered = value.lower()
        return any(keyword in lowered for keyword in keywords)

    def _is_protected_boundary_layer(self, layer_name: str | None) -> bool:
        return self._matches_keyword_group(layer_name, self.PROTECTED_BOUNDARY_LAYER_KEYWORDS)

    def _should_force_remove_layer(self, layer_name: str | None) -> bool:
        if self._is_protected_boundary_layer(layer_name):
            return False
        return self._matches_keyword_group(layer_name, self.FORCED_DELETE_LAYER_KEYWORDS)

    def _is_title_block_helper_entity(
        self,
        flattened: FlattenedEntity,
        title_bbox: Dict[str, float],
        drawing_bbox: Dict[str, float],
        layer_type_stats: Dict[str, Dict[str, int]],
        structured_line_threshold: float,
    ) -> bool:
        dxftype = flattened.entity.dxftype()
        layer_name = str(flattened.effective_layer or "")
        linetype = str(flattened.effective_linetype or "")

        if dxftype in self.parser.TITLE_SIGNAL_TYPES or dxftype in self.parser.FORCED_DELETE_TYPES:
            return True
        if self._is_protected_boundary_layer(layer_name):
            return False
        if self._should_preserve_title_block_geometry(flattened, drawing_bbox):
            return False

        soft_evidence = 0
        if self._matches_keyword_group(layer_name, self.ANNOTATION_LAYER_KEYWORDS):
            soft_evidence += 1
        if self._matches_keyword_group(linetype, tuple(keyword.lower() for keyword in self.ANNOTATION_LINETYPE_KEYWORDS)):
            soft_evidence += 1
        if self._layer_annotation_ratio(layer_name, layer_type_stats) >= self.ANNOTATION_LAYER_RATIO_THRESHOLD:
            soft_evidence += 1
        if any(self._matches_keyword_group(name, self.ANNOTATION_BLOCK_KEYWORDS) for name in flattened.block_path):
            soft_evidence += 1

        if soft_evidence >= 2:
            return True

        if self.parser._is_closed_rectangle(flattened):
            entity_bbox = self._entity_bbox_dict(flattened)
            entity_area = self._bbox_area(entity_bbox) if entity_bbox is not None else 0.0
            title_area = self._bbox_area(title_bbox)
            return soft_evidence >= 1 or entity_area >= title_area * 0.6

        if dxftype == "LINE":
            return self._is_title_block_table_line(flattened, title_bbox, structured_line_threshold)

        return False

    def _is_title_block_table_line(
        self,
        flattened: FlattenedEntity,
        title_bbox: Dict[str, float],
        structured_line_threshold: float,
    ) -> bool:
        if flattened.entity.dxftype() != "LINE":
            return False

        start = self.parser._apply_transform(
            (flattened.entity.dxf.start.x, flattened.entity.dxf.start.y),
            flattened.transform,
        )
        end = self.parser._apply_transform(
            (flattened.entity.dxf.end.x, flattened.entity.dxf.end.y),
            flattened.transform,
        )
        dx = abs(end.x - start.x)
        dy = abs(end.y - start.y)
        length = math.hypot(dx, dy)
        tolerance = max(
            (title_bbox["maxX"] - title_bbox["minX"]) * 0.01,
            (title_bbox["maxY"] - title_bbox["minY"]) * 0.01,
            1.0,
        )
        is_horizontal = dy <= tolerance
        is_vertical = dx <= tolerance
        if not (is_horizontal or is_vertical):
            return False
        if self._is_edge_spanning_line(start, end, title_bbox, tolerance):
            return False

        orientation_span = (
            title_bbox["maxX"] - title_bbox["minX"]
            if is_horizontal
            else title_bbox["maxY"] - title_bbox["minY"]
        )
        max_line_length = min(
            structured_line_threshold,
            max(orientation_span * self.TITLE_BLOCK_LINE_SPAN_RATIO, tolerance * 2.0),
        )
        return length <= max_line_length

    def _is_edge_spanning_line(
        self,
        start,
        end,
        bbox: Dict[str, float],
        tolerance: float,
    ) -> bool:
        dx = abs(end.x - start.x)
        dy = abs(end.y - start.y)
        length = math.hypot(dx, dy)
        width = bbox["maxX"] - bbox["minX"]
        height = bbox["maxY"] - bbox["minY"]

        if dy <= tolerance:
            y = (start.y + end.y) / 2.0
            near_edge = abs(y - bbox["minY"]) <= tolerance or abs(y - bbox["maxY"]) <= tolerance
            return near_edge and length >= width * self.TITLE_BLOCK_EDGE_SPAN_RATIO

        if dx <= tolerance:
            x = (start.x + end.x) / 2.0
            near_edge = abs(x - bbox["minX"]) <= tolerance or abs(x - bbox["maxX"]) <= tolerance
            return near_edge and length >= height * self.TITLE_BLOCK_EDGE_SPAN_RATIO

        return False

    def _bbox_touches_drawing_edge(
        self,
        bbox: Dict[str, float],
        drawing_bbox: Dict[str, float],
    ) -> bool:
        tolerance = max(
            (drawing_bbox["maxX"] - drawing_bbox["minX"]) * 0.01,
            (drawing_bbox["maxY"] - drawing_bbox["minY"]) * 0.01,
            1.0,
        )
        return (
            abs(bbox["minX"] - drawing_bbox["minX"]) <= tolerance
            or abs(bbox["minY"] - drawing_bbox["minY"]) <= tolerance
            or abs(bbox["maxX"] - drawing_bbox["maxX"]) <= tolerance
            or abs(bbox["maxY"] - drawing_bbox["maxY"]) <= tolerance
        )

    def _bbox_edge_anchor_count(
        self,
        bbox: Dict[str, float],
        drawing_bbox: Dict[str, float],
    ) -> int:
        tolerance = max(
            (drawing_bbox["maxX"] - drawing_bbox["minX"]) * self.FRAME_OUTER_EDGE_TOLERANCE_RATIO,
            (drawing_bbox["maxY"] - drawing_bbox["minY"]) * self.FRAME_OUTER_EDGE_TOLERANCE_RATIO,
            2.0,
        )
        return sum(
            (
                abs(bbox["minX"] - drawing_bbox["minX"]) <= tolerance,
                abs(bbox["maxX"] - drawing_bbox["maxX"]) <= tolerance,
                abs(bbox["minY"] - drawing_bbox["minY"]) <= tolerance,
                abs(bbox["maxY"] - drawing_bbox["maxY"]) <= tolerance,
            )
        )

    def _bbox_touches_corner(
        self,
        bbox: Dict[str, float],
        drawing_bbox: Dict[str, float],
    ) -> bool:
        tolerance = max(
            (drawing_bbox["maxX"] - drawing_bbox["minX"]) * 0.01,
            (drawing_bbox["maxY"] - drawing_bbox["minY"]) * 0.01,
            1.0,
        )
        touches_left = abs(bbox["minX"] - drawing_bbox["minX"]) <= tolerance
        touches_right = abs(bbox["maxX"] - drawing_bbox["maxX"]) <= tolerance
        touches_bottom = abs(bbox["minY"] - drawing_bbox["minY"]) <= tolerance
        touches_top = abs(bbox["maxY"] - drawing_bbox["maxY"]) <= tolerance
        return (
            (touches_left and touches_bottom)
            or (touches_left and touches_top)
            or (touches_right and touches_bottom)
            or (touches_right and touches_top)
        )

    def _calculate_flattened_bbox(self, flattened_entities: List[FlattenedEntity]) -> Dict[str, float]:
        return self.parser._calculate_flattened_bbox(flattened_entities)

    def _entity_bbox_dict(self, flattened: FlattenedEntity) -> Dict[str, float] | None:
        return self.parser._entity_bbox_dict(flattened)

    def _flattened_entity_center(self, flattened: FlattenedEntity):
        return self.parser._flattened_entity_center(flattened)

    def _flattened_entity_within_bbox(
        self,
        flattened: FlattenedEntity,
        bbox: Dict[str, float],
    ) -> bool:
        entity_bbox = self._entity_bbox_dict(flattened)
        if entity_bbox is None:
            return False
        return self._bbox_contains_bbox(bbox, entity_bbox, margin=1e-6)

    def _should_preserve_title_block_geometry(
        self,
        flattened: FlattenedEntity,
        drawing_bbox: Dict[str, float],
    ) -> bool:
        if flattened.entity.dxftype() not in self.parser.GEOMETRY_ENTITY_TYPES:
            return False
        if self.parser._is_closed_rectangle(flattened):
            return False

        entity_bbox = self._entity_bbox_dict(flattened)
        if entity_bbox is None:
            return False

        drawing_diagonal = math.hypot(
            drawing_bbox["maxX"] - drawing_bbox["minX"],
            drawing_bbox["maxY"] - drawing_bbox["minY"],
        )
        span_threshold = drawing_diagonal * self.TITLE_BLOCK_WALL_SPAN_RATIO
        width = entity_bbox["maxX"] - entity_bbox["minX"]
        height = entity_bbox["maxY"] - entity_bbox["minY"]
        longest_span = max(width, height)

        if flattened.entity.dxftype() == "LINE":
            return self._flattened_line_length(flattened) >= span_threshold

        return longest_span >= span_threshold

    def _bbox_contains_bbox(
        self,
        outer: Dict[str, float],
        inner: Dict[str, float],
        margin: float = 0.0,
    ) -> bool:
        return (
            inner["minX"] >= outer["minX"] - margin
            and inner["minY"] >= outer["minY"] - margin
            and inner["maxX"] <= outer["maxX"] + margin
            and inner["maxY"] <= outer["maxY"] + margin
        )

    def _bbox_intersects_bbox(
        self,
        left: Dict[str, float],
        right: Dict[str, float],
    ) -> bool:
        return not (
            left["maxX"] < right["minX"]
            or left["minX"] > right["maxX"]
            or left["maxY"] < right["minY"]
            or left["minY"] > right["maxY"]
        )

    def _bbox_contains_point(
        self,
        bbox: Dict[str, float],
        point: Tuple[float, float],
        margin: float = 0.0,
    ) -> bool:
        return (
            bbox["minX"] - margin <= point[0] <= bbox["maxX"] + margin
            and bbox["minY"] - margin <= point[1] <= bbox["maxY"] + margin
        )

    def _bbox_intersection_area(
        self,
        left: Dict[str, float],
        right: Dict[str, float],
    ) -> float:
        min_x = max(left["minX"], right["minX"])
        min_y = max(left["minY"], right["minY"])
        max_x = min(left["maxX"], right["maxX"])
        max_y = min(left["maxY"], right["maxY"])
        if max_x <= min_x or max_y <= min_y:
            return 0.0
        return (max_x - min_x) * (max_y - min_y)

    def _bbox_area(self, bbox: Dict[str, float]) -> float:
        return max(0.0, bbox["maxX"] - bbox["minX"]) * max(0.0, bbox["maxY"] - bbox["minY"])
