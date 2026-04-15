"""
Top-down outline extraction (STEP 6-8, V2).

This path approximates the building footprint by buffering snapped linework,
closing small gaps with unary union, and shrinking back toward the original
outline. It is designed to be more robust to open exterior walls than the
polygonize-based legacy pipeline.
"""
import logging
import math
from statistics import median
from typing import Dict, List, Optional, Tuple

from shapely import concave_hull
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Polygon
from shapely.ops import unary_union

from core.parser import Segment

logger = logging.getLogger(__name__)


class OutlineExtractorV2:
    """Extract a footprint directly from snapped linework."""

    def __init__(
        self,
        min_wall_thickness: float = 5.0,
        max_wall_thickness: float = 300.0,
        orthogonal_tolerance_deg: float = 15.0,
        parallel_tolerance_deg: float = 10.0,
        concave_hull_ratio: float = 0.3,
        courtyard_hole_ratio: float = 0.08,
        max_courtyard_hole_ratio: float = 0.2,
        max_courtyard_holes: int = 2,
        opening_radius_ratio: float = 0.75,
        opening_min_area_ratio: float = 0.9,
        opening_min_bbox_ratio: float = 0.94,
        opening_max_hull_ratio_delta: float = 0.08,
        closing_max_area_ratio: float = 1.08,
        closing_max_bbox_ratio: float = 1.03,
        closing_max_hull_ratio_delta: float = 0.08,
        bridge_min_span_ratio: float = 4.0,
        bridge_max_span_ratio: float = 60.0,
        bridge_min_deviation_ratio: float = 0.5,
        bridge_max_deviation_ratio: float = 8.0,
        bridge_min_area_ratio: float = 5.0,
        bridge_max_area_ratio: float = 180.0,
        bridge_boxy_area_ratio: float = 4.0,
        bridge_arc_path_ratio: float = 1.3,
        bridge_bbox_min_ratio: float = 0.9,
        bridge_bbox_max_ratio: float = 1.03,
        bridge_max_hull_ratio_delta: float = 0.08,
        curved_bridge_min_vertices: int = 5,
        curved_bridge_min_turn_deg: float = 55.0,
        curved_bridge_max_turn_deg: float = 125.0,
        curved_bridge_max_step_turn_deg: float = 45.0,
    ):
        self.min_wall_thickness = min_wall_thickness
        self.max_wall_thickness = max_wall_thickness
        self.orthogonal_tolerance_deg = orthogonal_tolerance_deg
        self.parallel_tolerance_deg = parallel_tolerance_deg
        self.concave_hull_ratio = concave_hull_ratio
        self.courtyard_hole_ratio = courtyard_hole_ratio
        self.max_courtyard_hole_ratio = max_courtyard_hole_ratio
        self.max_courtyard_holes = max_courtyard_holes
        self.opening_radius_ratio = opening_radius_ratio
        self.opening_min_area_ratio = opening_min_area_ratio
        self.opening_min_bbox_ratio = opening_min_bbox_ratio
        self.opening_max_hull_ratio_delta = opening_max_hull_ratio_delta
        self.closing_max_area_ratio = closing_max_area_ratio
        self.closing_max_bbox_ratio = closing_max_bbox_ratio
        self.closing_max_hull_ratio_delta = closing_max_hull_ratio_delta
        self.bridge_min_span_ratio = bridge_min_span_ratio
        self.bridge_max_span_ratio = bridge_max_span_ratio
        self.bridge_min_deviation_ratio = bridge_min_deviation_ratio
        self.bridge_max_deviation_ratio = bridge_max_deviation_ratio
        self.bridge_min_area_ratio = bridge_min_area_ratio
        self.bridge_max_area_ratio = bridge_max_area_ratio
        self.bridge_boxy_area_ratio = bridge_boxy_area_ratio
        self.bridge_arc_path_ratio = bridge_arc_path_ratio
        self.bridge_bbox_min_ratio = bridge_bbox_min_ratio
        self.bridge_bbox_max_ratio = bridge_bbox_max_ratio
        self.bridge_max_hull_ratio_delta = bridge_max_hull_ratio_delta
        self.curved_bridge_min_vertices = curved_bridge_min_vertices
        self.curved_bridge_min_turn_deg = curved_bridge_min_turn_deg
        self.curved_bridge_max_turn_deg = curved_bridge_max_turn_deg
        self.curved_bridge_max_step_turn_deg = curved_bridge_max_step_turn_deg

    def extract_boundary(self, segments: List[Segment]) -> Tuple[Optional[Polygon], Dict]:
        """
        Extract a boundary polygon from snapped segments.

        Args:
            segments: Snapped/noded segments in normalized millimeter units

        Returns:
            Tuple of (polygon, metadata)
        """
        if not segments:
            logger.warning("No segments provided to OutlineExtractorV2")
            return None, {"error": "No segments provided"}

        lines = self._segments_to_linestrings(segments)
        if not lines:
            logger.warning("No valid line strings available for outline extraction")
            return None, {"error": "No valid line strings available"}

        radius, estimate_metadata = self._estimate_wall_half_thickness(segments)
        logger.info(
            "OutlineExtractorV2 using buffer radius %.2f mm via %s",
            radius,
            estimate_metadata["method"],
        )

        buffered = [
            line.buffer(radius, cap_style="square", join_style="mitre")
            for line in lines
        ]
        merged = unary_union(buffered)
        merged_polygon = self._select_polygon(merged)
        if merged_polygon is None:
            logger.error("Buffered union did not produce a polygonal geometry")
            return None, {"error": "Buffered union did not produce a polygon"}

        eroded_geometry, erosion_metadata = self._erode_with_guard(merged, radius)
        if eroded_geometry is None:
            logger.error("Morphological erosion failed to recover a polygon")
            return None, {"error": "Erosion failed to recover a polygon"}

        eroded_polygon, footprint_metadata = self._assemble_footprint(eroded_geometry)
        if eroded_polygon is None:
            logger.error("Failed to assemble footprint polygon from eroded geometry")
            return None, {"error": "Failed to assemble footprint polygon"}

        concave_used = False
        if self._looks_over_smoothed(eroded_polygon, merged_polygon):
            refined = self._apply_concave_fallback(merged_polygon)
            if refined is not None:
                eroded_polygon = refined
                concave_used = True

        opened_polygon, opening_metadata = self._apply_opening_with_guard(
            eroded_polygon,
            wall_thickness=radius * 2.0,
        )
        closed_polygon, closing_metadata = self._apply_closing_with_guard(
            opened_polygon,
            wall_thickness=radius * 2.0,
        )
        bridged_polygon, bridge_metadata = self._apply_bridge_with_guard(
            closed_polygon,
            wall_thickness=radius * 2.0,
        )
        eroded_polygon = bridged_polygon

        metadata = {
            "method": "outline_v2",
            "buffer_radius": radius,
            "estimated_wall_thickness": radius * 2.0,
            "estimate_method": estimate_metadata["method"],
            "estimate_candidates": estimate_metadata["candidate_count"],
            "merged_area": merged_polygon.area,
            "result_area": eroded_polygon.area,
            "concave_hull_fallback": concave_used,
        }
        metadata.update(erosion_metadata)
        metadata.update(footprint_metadata)
        metadata.update(opening_metadata)
        metadata.update(closing_metadata)
        metadata.update(bridge_metadata)
        return eroded_polygon, metadata

    def _segments_to_linestrings(self, segments: List[Segment]) -> List[LineString]:
        lines: List[LineString] = []
        for segment in segments:
            start = segment.start.to_2d()
            end = segment.end.to_2d()
            if start == end:
                continue
            lines.append(LineString([start, end]))
        return lines

    def _estimate_wall_half_thickness(self, segments: List[Segment]) -> Tuple[float, Dict]:
        connector_lengths = self._estimate_from_orthogonal_connectors(segments)
        if connector_lengths:
            thickness = self._clamp_wall_thickness(median(connector_lengths))
            return thickness / 2.0, {
                "method": "orthogonal_connectors",
                "candidate_count": len(connector_lengths),
            }

        parallel_distances = self._estimate_from_parallel_pairs(segments)
        if parallel_distances:
            thickness = self._clamp_wall_thickness(median(parallel_distances))
            return thickness / 2.0, {
                "method": "parallel_pairs",
                "candidate_count": len(parallel_distances),
            }

        bbox_thickness = self._estimate_from_bbox(segments)
        return bbox_thickness / 2.0, {
            "method": "bbox_fallback",
            "candidate_count": 0,
        }

    def _estimate_from_orthogonal_connectors(self, segments: List[Segment]) -> List[float]:
        endpoint_map: Dict[Tuple[float, float], List[int]] = {}
        for index, segment in enumerate(segments):
            endpoint_map.setdefault(self._point_key(segment.start.to_2d()), []).append(index)
            endpoint_map.setdefault(self._point_key(segment.end.to_2d()), []).append(index)

        candidates: List[float] = []
        for index, segment in enumerate(segments):
            length = segment.length()
            if not self._is_plausible_wall_thickness(length):
                continue

            start_neighbors = [
                segments[neighbor]
                for neighbor in endpoint_map[self._point_key(segment.start.to_2d())]
                if neighbor != index
            ]
            end_neighbors = [
                segments[neighbor]
                for neighbor in endpoint_map[self._point_key(segment.end.to_2d())]
                if neighbor != index
            ]

            if not start_neighbors or not end_neighbors:
                continue

            segment_angle = self._segment_angle(segment)
            found_match = False
            for start_neighbor in start_neighbors:
                if start_neighbor.length() < length * 1.25:
                    continue
                if not self._is_orthogonal(segment_angle, self._segment_angle(start_neighbor)):
                    continue

                for end_neighbor in end_neighbors:
                    if end_neighbor.length() < length * 1.25:
                        continue
                    end_angle = self._segment_angle(end_neighbor)
                    if not self._is_orthogonal(segment_angle, end_angle):
                        continue
                    if not self._is_parallel(self._segment_angle(start_neighbor), end_angle):
                        continue

                    found_match = True
                    break

                if found_match:
                    break

            if found_match:
                candidates.append(length)

        return candidates

    def _estimate_from_parallel_pairs(self, segments: List[Segment]) -> List[float]:
        filtered = [segment for segment in segments if segment.length() > self.min_wall_thickness * 1.5]
        filtered.sort(key=lambda segment: segment.length(), reverse=True)
        filtered = filtered[:200]

        distances: List[float] = []
        pair_budget = 6000
        pair_count = 0

        for left_index, left in enumerate(filtered):
            left_angle = self._segment_angle(left)
            left_axis = self._unit_vector(left)
            left_normal = (-left_axis[1], left_axis[0])
            left_mid = self._segment_midpoint(left)
            left_interval = self._project_interval(left, left_axis)

            for right in filtered[left_index + 1:]:
                pair_count += 1
                if pair_count > pair_budget:
                    return distances

                right_angle = self._segment_angle(right)
                if not self._is_parallel(left_angle, right_angle):
                    continue

                right_interval = self._project_interval(right, left_axis)
                overlap = self._interval_overlap(left_interval, right_interval)
                if overlap < min(left.length(), right.length()) * 0.3:
                    continue

                right_mid = self._segment_midpoint(right)
                distance = abs(
                    (right_mid[0] - left_mid[0]) * left_normal[0]
                    + (right_mid[1] - left_mid[1]) * left_normal[1]
                )

                if self._is_plausible_wall_thickness(distance):
                    distances.append(distance)

        return distances

    def _estimate_from_bbox(self, segments: List[Segment]) -> float:
        xs = [coord for segment in segments for coord in (segment.start.x, segment.end.x)]
        ys = [coord for segment in segments for coord in (segment.start.y, segment.end.y)]
        diagonal = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
        estimated = diagonal * 0.003
        return self._clamp_wall_thickness(estimated)

    def _erode_with_guard(self, geometry, radius: float):
        erosion_radius = radius
        eroded = geometry.buffer(-erosion_radius, join_style="mitre")
        polygon = self._select_polygon(eroded)

        if polygon is None or polygon.is_empty or polygon.area < geometry.area * 0.5:
            erosion_radius = radius * 0.5
            eroded = geometry.buffer(-erosion_radius, join_style="mitre")
            polygon = self._select_polygon(eroded)

        if polygon is None or polygon.is_empty:
            eroded = geometry
            erosion_radius = 0.0

        metadata = {
            "erosion_radius": erosion_radius,
            "erosion_retried": erosion_radius not in (0.0, radius),
        }
        return eroded, metadata

    def _apply_concave_fallback(self, polygon: Polygon) -> Optional[Polygon]:
        try:
            refined = concave_hull(polygon, ratio=self.concave_hull_ratio)
        except Exception as exc:
            logger.warning("concave_hull fallback failed: %s", exc)
            return None

        refined_polygon = self._select_polygon(refined)
        if refined_polygon is None or refined_polygon.is_empty:
            return None

        logger.info("Applied concave_hull fallback with ratio %.2f", self.concave_hull_ratio)
        return refined_polygon

    def _apply_opening_with_guard(
        self,
        polygon: Polygon,
        wall_thickness: float,
    ) -> Tuple[Polygon, Dict]:
        metadata = {
            "opening_attempted": False,
            "opening_applied": False,
            "opening_radius": 0.0,
            "opening_area_ratio": 1.0,
            "opening_bbox_ratio": 1.0,
            "opening_hole_delta": 0,
            "opening_hull_ratio_delta": 0.0,
            "opening_rollback_reasons": [],
        }

        if polygon is None or polygon.is_empty or wall_thickness <= 0:
            metadata["opening_rollback_reasons"] = ["invalid_input"]
            return polygon, metadata

        radius = min(
            wall_thickness * self.opening_radius_ratio * 0.5,
            self.max_wall_thickness * 0.5,
        )
        if radius <= 0:
            metadata["opening_rollback_reasons"] = ["non_positive_radius"]
            return polygon, metadata

        metadata["opening_attempted"] = True
        metadata["opening_radius"] = radius

        opened = polygon.buffer(-radius, join_style="mitre").buffer(radius, join_style="mitre")
        opened_polygon = self._select_polygon(opened)
        if opened_polygon is None or opened_polygon.is_empty:
            metadata["opening_rollback_reasons"] = ["empty_result"]
            logger.info("Opening rollback: empty result for radius %.2f", radius)
            return polygon, metadata

        area_ratio = opened_polygon.area / polygon.area if polygon.area > 0 else 1.0
        bbox_ratio = self._bbox_area_ratio(opened_polygon, polygon)
        hole_delta = len(opened_polygon.interiors) - len(polygon.interiors)
        hull_ratio_delta = abs(
            self._convex_hull_ratio(opened_polygon) - self._convex_hull_ratio(polygon)
        )

        metadata["opening_area_ratio"] = area_ratio
        metadata["opening_bbox_ratio"] = bbox_ratio
        metadata["opening_hole_delta"] = hole_delta
        metadata["opening_hull_ratio_delta"] = hull_ratio_delta

        rollback_reasons: List[str] = []
        if area_ratio < self.opening_min_area_ratio:
            rollback_reasons.append("area_drop_exceeded")
        if bbox_ratio < self.opening_min_bbox_ratio:
            rollback_reasons.append("bbox_drop_exceeded")
        if hole_delta != 0:
            rollback_reasons.append("hole_count_changed")
        if hull_ratio_delta > self.opening_max_hull_ratio_delta:
            rollback_reasons.append("hull_ratio_delta_exceeded")

        if rollback_reasons:
            metadata["opening_rollback_reasons"] = rollback_reasons
            logger.info(
                "Opening rollback: reasons=%s radius=%.2f area_ratio=%.3f bbox_ratio=%.3f "
                "hole_delta=%d hull_delta=%.3f",
                ",".join(rollback_reasons),
                radius,
                area_ratio,
                bbox_ratio,
                hole_delta,
                hull_ratio_delta,
            )
            return polygon, metadata

        metadata["opening_applied"] = True
        logger.info(
            "Applied guarded opening: radius=%.2f area_ratio=%.3f bbox_ratio=%.3f "
            "hole_delta=%d hull_delta=%.3f",
            radius,
            area_ratio,
            bbox_ratio,
            hole_delta,
            hull_ratio_delta,
        )
        return opened_polygon, metadata

    def _apply_closing_with_guard(
        self,
        polygon: Polygon,
        wall_thickness: float,
    ) -> Tuple[Polygon, Dict]:
        metadata = {
            "closing_attempted": False,
            "closing_applied": False,
            "closing_radius": 0.0,
            "closing_area_ratio": 1.0,
            "closing_bbox_ratio": 1.0,
            "closing_hole_delta": 0,
            "closing_hull_ratio_delta": 0.0,
            "closing_rollback_reasons": [],
        }

        if polygon is None or polygon.is_empty or wall_thickness <= 0:
            metadata["closing_rollback_reasons"] = ["invalid_input"]
            return polygon, metadata

        radius = min(
            wall_thickness * self.opening_radius_ratio * 0.5,
            self.max_wall_thickness * 0.5,
        )
        if radius <= 0:
            metadata["closing_rollback_reasons"] = ["non_positive_radius"]
            return polygon, metadata

        metadata["closing_attempted"] = True
        metadata["closing_radius"] = radius

        closed = polygon.buffer(radius, join_style="mitre").buffer(-radius, join_style="mitre")
        closed_polygon = self._select_polygon(closed)
        if closed_polygon is None or closed_polygon.is_empty:
            metadata["closing_rollback_reasons"] = ["empty_result"]
            logger.info("Closing rollback: empty result for radius %.2f", radius)
            return polygon, metadata

        area_ratio = closed_polygon.area / polygon.area if polygon.area > 0 else 1.0
        bbox_ratio = self._bbox_area_ratio(closed_polygon, polygon)
        hole_delta = len(closed_polygon.interiors) - len(polygon.interiors)
        hull_ratio_delta = abs(
            self._convex_hull_ratio(closed_polygon) - self._convex_hull_ratio(polygon)
        )

        metadata["closing_area_ratio"] = area_ratio
        metadata["closing_bbox_ratio"] = bbox_ratio
        metadata["closing_hole_delta"] = hole_delta
        metadata["closing_hull_ratio_delta"] = hull_ratio_delta

        rollback_reasons: List[str] = []
        if area_ratio > self.closing_max_area_ratio:
            rollback_reasons.append("area_growth_exceeded")
        if bbox_ratio > self.closing_max_bbox_ratio:
            rollback_reasons.append("bbox_growth_exceeded")
        if hole_delta != 0:
            rollback_reasons.append("hole_count_changed")
        if hull_ratio_delta > self.closing_max_hull_ratio_delta:
            rollback_reasons.append("hull_ratio_delta_exceeded")

        if rollback_reasons:
            metadata["closing_rollback_reasons"] = rollback_reasons
            logger.info(
                "Closing rollback: reasons=%s radius=%.2f area_ratio=%.3f bbox_ratio=%.3f "
                "hole_delta=%d hull_delta=%.3f",
                ",".join(rollback_reasons),
                radius,
                area_ratio,
                bbox_ratio,
                hole_delta,
                hull_ratio_delta,
            )
            return polygon, metadata

        metadata["closing_applied"] = True
        logger.info(
            "Applied guarded closing: radius=%.2f area_ratio=%.3f bbox_ratio=%.3f "
            "hole_delta=%d hull_delta=%.3f",
            radius,
            area_ratio,
            bbox_ratio,
            hole_delta,
            hull_ratio_delta,
        )
        return closed_polygon, metadata

    def _apply_bridge_with_guard(
        self,
        polygon: Polygon,
        wall_thickness: float,
    ) -> Tuple[Polygon, Dict]:
        metadata = {
            "bridge_attempted": False,
            "bridge_candidates": 0,
            "bridge_applied_count": 0,
            "bridge_rollback_reasons": [],
            "bridge_logs": [],
            "bridge_last_candidate": None,
            "bridge_last_candidate_source": None,
            "bridge_last_candidate_bounds": None,
            "bridge_last_candidate_metrics": None,
            "bridge_cv_fallback_recommended": False,
            "bridge_cv_fallback_trigger_reasons": [],
        }

        if polygon is None or polygon.is_empty or wall_thickness <= 0:
            metadata["bridge_rollback_reasons"] = ["invalid_input"]
            return polygon, metadata

        current = polygon
        applied = 0
        rollback_reasons: List[str] = []
        bridge_logs: List[str] = []

        for _ in range(8):
            candidate = self._find_bridge_candidate(current, wall_thickness)
            if candidate is None:
                break

            metadata["bridge_attempted"] = True
            metadata["bridge_candidates"] += 1
            candidate_snapshot = self._bridge_candidate_snapshot(current, candidate)
            metadata["bridge_last_candidate"] = candidate_snapshot
            metadata["bridge_last_candidate_source"] = candidate_snapshot["source"]
            metadata["bridge_last_candidate_bounds"] = candidate_snapshot["bounds"]
            metadata["bridge_last_candidate_metrics"] = candidate_snapshot["metrics"]
            candidate_log = (
                f"source={candidate.get('source', 'unknown')} "
                f"start={candidate['start']} end={candidate['end']} "
                f"span={candidate['span_ratio']:.2f}t dev={candidate['deviation_ratio']:.2f}t "
                f"area={candidate['area_ratio']:.2f}t2 path={candidate['path_ratio']:.2f}"
            )

            bridged = self._bridge_candidate(current, candidate["start"], candidate["end"])
            if bridged is None:
                rollback_reasons.append("invalid_bridge_geometry")
                bridge_logs.append(f"rollback:{candidate_log}:invalid")
                break

            bbox_ratio = self._bbox_area_ratio(bridged, current)
            hull_ratio_delta = abs(
                self._convex_hull_ratio(bridged) - self._convex_hull_ratio(current)
            )
            hole_delta = len(bridged.interiors) - len(current.interiors)

            if bbox_ratio < self.bridge_bbox_min_ratio:
                rollback_reasons.append("bbox_drop_exceeded")
                bridge_logs.append(f"rollback:{candidate_log}:bbox_drop={bbox_ratio:.3f}")
                break
            if bbox_ratio > self.bridge_bbox_max_ratio:
                rollback_reasons.append("bbox_growth_exceeded")
                bridge_logs.append(f"rollback:{candidate_log}:bbox_growth={bbox_ratio:.3f}")
                break
            if hole_delta != 0:
                rollback_reasons.append("hole_count_changed")
                bridge_logs.append(f"rollback:{candidate_log}:hole_delta={hole_delta}")
                break
            if hull_ratio_delta > self.bridge_max_hull_ratio_delta:
                rollback_reasons.append("hull_ratio_delta_exceeded")
                bridge_logs.append(f"rollback:{candidate_log}:hull_delta={hull_ratio_delta:.3f}")
                break

            bridge_logs.append(f"applied:{candidate_log}")
            current = bridged
            applied += 1

        metadata["bridge_applied_count"] = applied
        metadata["bridge_rollback_reasons"] = rollback_reasons
        metadata["bridge_logs"] = bridge_logs
        should_recommend_cv, trigger_reasons = self._should_recommend_cv_bridge_fallback(metadata)
        metadata["bridge_cv_fallback_recommended"] = should_recommend_cv
        metadata["bridge_cv_fallback_trigger_reasons"] = trigger_reasons
        if applied:
            logger.info("Applied %d polygon bridges: %s", applied, "; ".join(bridge_logs[:5]))
        elif rollback_reasons:
            logger.info("Bridge rollback: %s", "; ".join(bridge_logs[:5]))
        return current, metadata

    def _find_bridge_candidate(
        self,
        polygon: Polygon,
        wall_thickness: float,
    ) -> Optional[Dict]:
        candidate = self._find_reflex_bridge_candidate(polygon, wall_thickness)
        if candidate is not None:
            candidate["source"] = "reflex"
            return candidate

        candidate = self._find_curved_bridge_candidate(polygon, wall_thickness)
        if candidate is not None:
            candidate["source"] = "curved"
        return candidate

    def _find_reflex_bridge_candidate(
        self,
        polygon: Polygon,
        wall_thickness: float,
    ) -> Optional[Dict]:
        coords = list(polygon.exterior.coords[:-1])
        if len(coords) < 6:
            return None

        reflex_indices = self._find_reflex_vertices(coords)
        if len(reflex_indices) < 2:
            return None

        best_candidate = None
        for offset, start in enumerate(reflex_indices):
            end = reflex_indices[(offset + 1) % len(reflex_indices)]
            chain = self._slice_ring(coords, start, end)
            if len(chain) < 3 or len(chain) > 16:
                continue

            chord = self._distance(chain[0], chain[-1])
            if chord <= 0:
                continue

            span_ratio = chord / wall_thickness
            if not (self.bridge_min_span_ratio <= span_ratio <= self.bridge_max_span_ratio):
                continue

            path_length = self._path_length(chain)
            path_ratio = path_length / chord if chord > 0 else float("inf")
            deviation = max(self._point_line_distance(point, chain[0], chain[-1]) for point in chain[1:-1]) if len(chain) > 2 else 0.0
            deviation_ratio = deviation / wall_thickness
            if not (self.bridge_min_deviation_ratio <= deviation_ratio <= self.bridge_max_deviation_ratio):
                continue

            feature_area = abs(Polygon(chain).area)
            area_ratio = feature_area / (wall_thickness * wall_thickness)
            if not (self.bridge_min_area_ratio <= area_ratio <= self.bridge_max_area_ratio):
                continue

            if path_ratio > self.bridge_arc_path_ratio and area_ratio > self.bridge_boxy_area_ratio:
                continue

            candidate = {
                "start": start,
                "end": end,
                "span_ratio": span_ratio,
                "path_ratio": path_ratio,
                "deviation_ratio": deviation_ratio,
                "area_ratio": area_ratio,
            }
            if best_candidate is None or candidate["area_ratio"] < best_candidate["area_ratio"]:
                best_candidate = candidate

        return best_candidate

    def _find_curved_bridge_candidate(
        self,
        polygon: Polygon,
        wall_thickness: float,
    ) -> Optional[Dict]:
        coords = list(polygon.exterior.coords[:-1])
        if len(coords) < self.curved_bridge_min_vertices:
            return None

        best_candidate = None
        ring_length = len(coords)
        max_span = min(ring_length - 1, 18)

        for start in range(ring_length):
            cumulative_turn = 0.0
            turn_sign = 0
            step_turns: List[float] = []
            turn_count = 0

            for step in range(2, max_span + 1):
                end = (start + step) % ring_length
                turn = self._signed_turn(coords[(start + step - 2) % ring_length], coords[(start + step - 1) % ring_length], coords[end])
                turn_deg = abs(math.degrees(turn))
                if turn_deg < 2.0:
                    continue

                current_sign = 1 if turn > 0 else -1
                if turn_sign == 0:
                    turn_sign = current_sign
                elif current_sign != turn_sign:
                    break

                cumulative_turn += turn_deg
                step_turns.append(turn_deg)
                turn_count += 1

                chain = self._slice_ring(coords, start, end)
                if len(chain) < self.curved_bridge_min_vertices:
                    continue
                if turn_count < 2:
                    continue
                if not (self.curved_bridge_min_turn_deg <= cumulative_turn <= self.curved_bridge_max_turn_deg):
                    continue
                smoothed_turns = step_turns[1:-1] if len(step_turns) > 2 else step_turns[:-1]
                max_smoothed_turn = max(smoothed_turns) if smoothed_turns else 0.0
                if max_smoothed_turn > self.curved_bridge_max_step_turn_deg:
                    continue

                chord = self._distance(chain[0], chain[-1])
                if chord <= 0:
                    continue
                span_ratio = chord / wall_thickness
                if not (self.bridge_min_span_ratio <= span_ratio <= self.bridge_max_span_ratio):
                    continue

                path_length = self._path_length(chain)
                path_ratio = path_length / chord
                deviation = max(
                    self._point_line_distance(point, chain[0], chain[-1])
                    for point in chain[1:-1]
                )
                deviation_ratio = deviation / wall_thickness
                if not (self.bridge_min_deviation_ratio <= deviation_ratio <= self.bridge_max_deviation_ratio):
                    continue

                feature_area = abs(Polygon(chain).area)
                area_ratio = feature_area / (wall_thickness * wall_thickness)
                if not (self.bridge_min_area_ratio <= area_ratio <= self.bridge_max_area_ratio):
                    continue

                score = (
                    abs(cumulative_turn - 90.0)
                    + abs(path_ratio - 1.12) * 40.0
                    + abs(span_ratio - 28.0) * 0.25
                    - min(turn_count, 8) * 2.0
                )
                candidate = {
                    "start": start,
                    "end": end,
                    "span_ratio": span_ratio,
                    "path_ratio": path_ratio,
                    "deviation_ratio": deviation_ratio,
                    "area_ratio": area_ratio,
                    "turn_deg": cumulative_turn,
                    "turn_count": turn_count,
                    "max_smoothed_turn_deg": max_smoothed_turn,
                    "score": score,
                }
                if best_candidate is None or candidate["score"] < best_candidate["score"]:
                    best_candidate = candidate

        return best_candidate

    def _bridge_candidate(
        self,
        polygon: Polygon,
        start: int,
        end: int,
    ) -> Optional[Polygon]:
        coords = list(polygon.exterior.coords[:-1])
        if len(coords) < 4:
            return None

        rotated = coords[start:] + coords[:start]
        rotated_end = (end - start) % len(coords)
        if rotated_end <= 0 or rotated_end >= len(rotated):
            return None

        new_exterior = rotated[:1] + rotated[rotated_end:]
        if len(new_exterior) < 3:
            return None

        bridged = Polygon(new_exterior, [list(interior.coords) for interior in polygon.interiors])
        if not bridged.is_valid:
            bridged = bridged.buffer(0)
            bridged = self._select_polygon(bridged)

        if bridged is None or bridged.is_empty:
            return None
        return bridged

    def _find_reflex_vertices(self, coords: List[Tuple[float, float]]) -> List[int]:
        orientation = self._ring_orientation(coords)
        if orientation == 0:
            return []

        reflex: List[int] = []
        epsilon = 1e-6
        count = len(coords)
        for index in range(count):
            prev_point = coords[index - 1]
            curr_point = coords[index]
            next_point = coords[(index + 1) % count]
            turn = self._cross(prev_point, curr_point, next_point) * orientation
            if turn < -epsilon:
                reflex.append(index)
        return reflex

    def _looks_over_smoothed(self, polygon: Polygon, merged_polygon: Polygon) -> bool:
        if polygon is None or merged_polygon is None:
            return False

        polygon_hull_ratio = self._convex_hull_ratio(polygon)
        merged_hull_ratio = self._convex_hull_ratio(merged_polygon)
        merged_shell_area = Polygon(merged_polygon.exterior).area

        # The original suggestion used polygon.area / convex_hull.area > 1.3,
        # but that ratio is bounded by 1.0. We instead look for a large jump
        # toward convexity after erosion, which is a practical signal that an
        # L- or U-shaped footprint may have been over-filled.
        return (
            polygon.area < merged_shell_area * 0.8
            and polygon_hull_ratio > 0.995
            and merged_hull_ratio < 0.8
            and len(polygon.interiors) == 0
        )

    def _select_polygon(self, geometry) -> Optional[Polygon]:
        if geometry is None or geometry.is_empty:
            return None

        if isinstance(geometry, Polygon):
            return geometry

        if isinstance(geometry, MultiPolygon):
            logger.warning(
                "Outline extraction produced MultiPolygon with %d parts; using largest",
                len(geometry.geoms),
            )
            return max(geometry.geoms, key=lambda geom: geom.area)

        if isinstance(geometry, GeometryCollection):
            polygons = [geom for geom in geometry.geoms if isinstance(geom, Polygon)]
            if polygons:
                return max(polygons, key=lambda geom: geom.area)

        return None

    def _collect_polygons(self, geometry) -> List[Polygon]:
        if geometry is None or geometry.is_empty:
            return []

        if isinstance(geometry, Polygon):
            return [geometry]

        if isinstance(geometry, MultiPolygon):
            return list(geometry.geoms)

        if isinstance(geometry, GeometryCollection):
            return [geom for geom in geometry.geoms if isinstance(geom, Polygon)]

        return []

    def _assemble_footprint(self, geometry) -> Tuple[Optional[Polygon], Dict]:
        polygons = self._collect_polygons(geometry)
        if not polygons:
            return None, {"footprint_parts": 0, "footprint_holes": 0}

        shell = max(polygons, key=lambda geom: Polygon(geom.exterior).area)
        shell_polygon = Polygon(shell.exterior)
        shell_area = shell_polygon.area
        min_hole_area = max((self.min_wall_thickness * 4.0) ** 2, 100.0)
        retained_holes: List[Polygon] = []

        shell_holes = sorted(
            (Polygon(interior) for interior in shell.interiors),
            key=lambda hole: hole.area,
            reverse=True,
        )

        # The largest interior on the outer shell is typically the occupied
        # floor space inside the exterior walls, so we fill it by default.
        for hole in shell_holes[1:]:
            if self._is_courtyard_candidate(hole, shell_area, min_hole_area):
                retained_holes.append(hole)

        for polygon in polygons:
            if polygon is shell or not shell_polygon.covers(polygon):
                continue

            for interior in polygon.interiors:
                hole = Polygon(interior)
                if self._is_courtyard_candidate(hole, shell_area, min_hole_area):
                    retained_holes.append(hole)

        retained_holes = sorted(retained_holes, key=lambda hole: hole.area, reverse=True)
        if self.max_courtyard_holes > 0:
            retained_holes = retained_holes[:self.max_courtyard_holes]

        holes = [list(hole.exterior.coords) for hole in retained_holes]

        footprint = Polygon(shell.exterior.coords, holes)
        if footprint.is_empty:
            return None, {"footprint_parts": len(polygons), "footprint_holes": len(holes)}

        if not footprint.is_valid:
            footprint = footprint.buffer(0)
            footprint = self._select_polygon(footprint)

        if footprint is None or footprint.is_empty:
            return None, {"footprint_parts": len(polygons), "footprint_holes": len(holes)}

        return footprint, {
            "footprint_parts": len(polygons),
            "footprint_holes": len(holes),
        }

    def _is_courtyard_candidate(
        self,
        hole: Polygon,
        shell_area: float,
        min_hole_area: float,
    ) -> bool:
        if hole.is_empty or not hole.is_valid:
            return False

        area_threshold = max(min_hole_area, shell_area * self.courtyard_hole_ratio)
        if hole.area < area_threshold:
            return False
        if hole.area > shell_area * self.max_courtyard_hole_ratio:
            return False

        min_x, min_y, max_x, max_y = hole.bounds
        width = max_x - min_x
        height = max_y - min_y
        if min(width, height) < self.min_wall_thickness * 12.0:
            return False

        hull = hole.convex_hull
        if hull.is_empty or hull.area == 0:
            return False

        compactness = hole.area / hull.area
        return compactness >= 0.55

    def _segment_angle(self, segment: Segment) -> float:
        dy = segment.end.y - segment.start.y
        dx = segment.end.x - segment.start.x
        return math.degrees(math.atan2(dy, dx)) % 180.0

    def _unit_vector(self, segment: Segment) -> Tuple[float, float]:
        dx = segment.end.x - segment.start.x
        dy = segment.end.y - segment.start.y
        length = math.hypot(dx, dy)
        if length == 0:
            return (1.0, 0.0)
        return (dx / length, dy / length)

    def _segment_midpoint(self, segment: Segment) -> Tuple[float, float]:
        return (
            (segment.start.x + segment.end.x) * 0.5,
            (segment.start.y + segment.end.y) * 0.5,
        )

    def _project_interval(self, segment: Segment, axis: Tuple[float, float]) -> Tuple[float, float]:
        start_projection = segment.start.x * axis[0] + segment.start.y * axis[1]
        end_projection = segment.end.x * axis[0] + segment.end.y * axis[1]
        return (min(start_projection, end_projection), max(start_projection, end_projection))

    def _interval_overlap(
        self,
        left: Tuple[float, float],
        right: Tuple[float, float],
    ) -> float:
        return max(0.0, min(left[1], right[1]) - max(left[0], right[0]))

    def _point_key(self, point: Tuple[float, float]) -> Tuple[float, float]:
        return (round(point[0], 6), round(point[1], 6))

    def _is_parallel(self, angle_a: float, angle_b: float) -> bool:
        delta = abs(angle_a - angle_b) % 180.0
        delta = min(delta, 180.0 - delta)
        return delta <= self.parallel_tolerance_deg

    def _is_orthogonal(self, angle_a: float, angle_b: float) -> bool:
        delta = abs(angle_a - angle_b) % 180.0
        delta = min(delta, 180.0 - delta)
        return abs(delta - 90.0) <= self.orthogonal_tolerance_deg

    def _is_plausible_wall_thickness(self, value: float) -> bool:
        return self.min_wall_thickness <= value <= self.max_wall_thickness

    def _clamp_wall_thickness(self, value: float) -> float:
        return max(self.min_wall_thickness, min(self.max_wall_thickness, value))

    def _convex_hull_ratio(self, polygon: Polygon) -> float:
        hull = polygon.convex_hull
        if hull.is_empty or hull.area == 0:
            return 1.0
        return polygon.area / hull.area

    def _bbox_area_ratio(self, polygon: Polygon, baseline: Polygon) -> float:
        baseline_bbox_area = self._polygon_bbox_area(baseline)
        if baseline_bbox_area == 0:
            return 1.0
        return self._polygon_bbox_area(polygon) / baseline_bbox_area

    def _polygon_bbox_area(self, polygon: Polygon) -> float:
        min_x, min_y, max_x, max_y = polygon.bounds
        return max(0.0, max_x - min_x) * max(0.0, max_y - min_y)

    def _ring_orientation(self, coords: List[Tuple[float, float]]) -> int:
        signed_area = 0.0
        for index, point in enumerate(coords):
            next_point = coords[(index + 1) % len(coords)]
            signed_area += point[0] * next_point[1] - next_point[0] * point[1]
        if signed_area > 0:
            return 1
        if signed_area < 0:
            return -1
        return 0

    def _slice_ring(
        self,
        coords: List[Tuple[float, float]],
        start: int,
        end: int,
    ) -> List[Tuple[float, float]]:
        if start <= end:
            return coords[start:end + 1]
        return coords[start:] + coords[:end + 1]

    def _path_length(self, coords: List[Tuple[float, float]]) -> float:
        return sum(
            self._distance(coords[index], coords[index + 1])
            for index in range(len(coords) - 1)
        )

    def _distance(self, left: Tuple[float, float], right: Tuple[float, float]) -> float:
        return math.hypot(right[0] - left[0], right[1] - left[1])

    def _bridge_candidate_snapshot(self, polygon: Polygon, candidate: Dict) -> Dict:
        coords = list(polygon.exterior.coords[:-1])
        chain = self._slice_ring(coords, candidate["start"], candidate["end"])
        bounds = self._coords_bounds(chain)
        metrics = {
            "span_ratio": float(candidate["span_ratio"]),
            "path_ratio": float(candidate["path_ratio"]),
            "deviation_ratio": float(candidate["deviation_ratio"]),
            "area_ratio": float(candidate["area_ratio"]),
            "turn_deg": float(candidate["turn_deg"]) if "turn_deg" in candidate else None,
        }
        return {
            "start": int(candidate["start"]),
            "end": int(candidate["end"]),
            "source": candidate.get("source", "unknown"),
            "bounds": bounds,
            "metrics": metrics,
        }

    def _coords_bounds(self, coords: List[Tuple[float, float]]) -> Optional[List[float]]:
        if not coords:
            return None
        xs = [point[0] for point in coords]
        ys = [point[1] for point in coords]
        return [min(xs), min(ys), max(xs), max(ys)]

    def _should_recommend_cv_bridge_fallback(self, metadata: Dict) -> Tuple[bool, List[str]]:
        if not metadata.get("bridge_attempted"):
            return False, []
        if metadata.get("bridge_applied_count", 0) != 0:
            return False, []
        if metadata.get("bridge_last_candidate_source") != "curved":
            return False, []

        allowed_reasons = {
            "bbox_drop_exceeded",
            "hull_ratio_delta_exceeded",
            "invalid_bridge_geometry",
        }
        trigger_reasons = [
            reason for reason in metadata.get("bridge_rollback_reasons", [])
            if reason in allowed_reasons
        ]
        return bool(trigger_reasons), trigger_reasons

    def _point_line_distance(
        self,
        point: Tuple[float, float],
        line_start: Tuple[float, float],
        line_end: Tuple[float, float],
    ) -> float:
        dx = line_end[0] - line_start[0]
        dy = line_end[1] - line_start[1]
        length = math.hypot(dx, dy)
        if length == 0:
            return self._distance(point, line_start)
        return abs(
            (point[0] - line_start[0]) * dy
            - (point[1] - line_start[1]) * dx
        ) / length

    def _cross(
        self,
        prev_point: Tuple[float, float],
        curr_point: Tuple[float, float],
        next_point: Tuple[float, float],
    ) -> float:
        return (
            (curr_point[0] - prev_point[0]) * (next_point[1] - curr_point[1])
            - (curr_point[1] - prev_point[1]) * (next_point[0] - curr_point[0])
        )

    def _signed_turn(
        self,
        prev_point: Tuple[float, float],
        curr_point: Tuple[float, float],
        next_point: Tuple[float, float],
    ) -> float:
        inbound = (
            curr_point[0] - prev_point[0],
            curr_point[1] - prev_point[1],
        )
        outbound = (
            next_point[0] - curr_point[0],
            next_point[1] - curr_point[1],
        )
        angle_in = math.atan2(inbound[1], inbound[0])
        angle_out = math.atan2(outbound[1], outbound[0])
        return (angle_out - angle_in + math.pi) % (2 * math.pi) - math.pi
