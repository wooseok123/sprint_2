"""
Graph Construction and Pruning (STEP 4-5)
Implements tolerance snapping with KD-tree + Union-Find, and dangling edge pruning.
"""
import math
import logging
from statistics import median
from typing import List, Dict, Tuple, Optional, Iterable, Set
from dataclasses import dataclass

import numpy as np
try:
    from scipy.spatial import KDTree
except ImportError:  # pragma: no cover - optional runtime acceleration
    KDTree = None
import networkx as nx
from shapely.geometry import LineString, box
from shapely.strtree import STRtree

from core.parser import Segment, Point

logger = logging.getLogger(__name__)


@dataclass
class GraphMetrics:
    """Metrics after graph construction and pruning."""
    node_count: int
    edge_count: int
    components: int
    max_degree: int
    pruned_edges: int
    pruned_percent: float
    removed_small_components: int = 0
    removed_small_component_edges: int = 0


class GraphProcessor:
    """
    Graph processor for tolerance snapping and pruning.
    Implements STEP 4: Tolerance snapping with KD-tree + Union-Find
    Implements STEP 5: Dangling edge pruning (degree=1 removal)
    """

    def __init__(self, bbox: Dict[str, float], adaptive_params: Dict[str, float] = None):
        """
        Initialize graph processor.

        Args:
            bbox: Bounding box {minX, minY, maxX, maxY}
            adaptive_params: Optional adaptive parameters
                {
                    'tolerance_global_percent': 0.1,  # 0.1% of bbox diagonal
                    'tolerance_local_percent': 1.0,   # 1% of avg segment length
                    'min_tolerance_mm': 0.001,
                    'max_tolerance_mm': 1.0
                }
        """
        self.bbox = bbox
        default_params = {
            'tolerance_global_percent': 0.1,
            'tolerance_local_percent': 1.0,
            'min_tolerance_mm': 0.001,
            'max_tolerance_mm': 1.0,
            'endpoint_extension_ratio': 1.5,
            'min_extension_mm': 25.0,
            'max_extension_mm': 120.0,
            'parallel_tolerance_deg': 12.0,
            'orthogonal_tolerance_deg': 12.0,
            'min_source_length_factor': 0.5,
            'max_extension_passes': 4,
            'corridor_padding_ratio': 0.15,
            'max_extension_segments': 50000,
            'max_extension_dangling_nodes': 4000,
            'terminal_branch_length_ratio': 12.0,
        }
        self.adaptive_params = {**default_params, **(adaptive_params or {})}

        self.tolerance = None
        self.graph = None
        self.latest_segments: List[Segment] = []
        self.extension_metadata = {
            "attempted": False,
            "applied_count": 0,
            "extension_length": 0.0,
            "estimate_method": "uninitialized",
            "skipped_reason": "",
            "applied_extensions": [],
        }

    def build_graph_and_snap(self, segments: List[Segment]) -> Tuple[nx.Graph, List[Segment]]:
        """
        Build graph and apply tolerance snapping.

        Args:
            segments: List of noded segments

        Returns:
            Tuple of (networkx.Graph, snapped_segments)
        """
        logger.info(f"Building graph from {len(segments)} segments")

        # Calculate adaptive tolerance
        self.tolerance = self._calculate_adaptive_tolerance(segments)
        logger.info(f"Adaptive tolerance: {self.tolerance:.6f}")

        # Apply tolerance snapping
        snapped_segments = self._apply_tolerance_snapping(segments)
        logger.info(f"Tolerance snapping: {len(segments)} → {len(snapped_segments)} segments")

        # Apply short directional extensions for likely under-shot wall endpoints
        snapped_segments = self._extend_dangling_endpoints(snapped_segments)
        if self.extension_metadata["attempted"]:
            logger.info(
                "Directional endpoint extension: length=%.2f mm via %s, applied=%d",
                self.extension_metadata["extension_length"],
                self.extension_metadata["estimate_method"],
                self.extension_metadata["applied_count"],
            )
        elif self.extension_metadata.get("skipped_reason"):
            logger.info(
                "Directional endpoint extension skipped: %s",
                self.extension_metadata["skipped_reason"],
            )

        # Build networkx graph
        self.graph = self._build_graph(snapped_segments)

        logger.info(f"Graph built: {self.graph.number_of_nodes()} nodes, {self.graph.number_of_edges()} edges")
        self.latest_segments = snapped_segments

        return self.graph, snapped_segments

    def prune_dangling_edges(self, max_iterations: int = 1000) -> GraphMetrics:
        """
        Iteratively remove short degree=1 spurs only.

        Args:
            max_iterations: Maximum pruning iterations to prevent infinite loops

        Returns:
            GraphMetrics with pruning statistics
        """
        if self.graph is None:
            raise ValueError("Graph not built. Call build_graph_and_snap first.")

        logger.info("Starting short-spur pruning...")

        initial_edges = self.graph.number_of_edges()
        iteration = 0
        total_pruned = 0
        max_prune_percent = 10.0
        max_prunable_edges = max(1, int(math.ceil(initial_edges * max_prune_percent / 100.0))) if initial_edges > 0 else 0
        spur_length_limit = self._calculate_spur_length_limit()
        wall_thickness, _ = self._estimate_wall_thickness(self.latest_segments) if self.latest_segments else (0.0, "unavailable")
        terminal_branch_length_limit = max(
            spur_length_limit,
            wall_thickness * self.adaptive_params["terminal_branch_length_ratio"],
        )

        while iteration < max_iterations:
            dangling_nodes = []
            for node in self.graph.nodes:
                if self.graph.degree(node) != 1:
                    continue

                neighbor = next(iter(self.graph.neighbors(node)))
                edge_data = self.graph.get_edge_data(node, neighbor) or {}
                edge_length = edge_data.get('length', float('inf'))
                if edge_length <= spur_length_limit or self._is_terminal_branch_candidate(
                    node,
                    neighbor,
                    edge_length,
                    terminal_branch_length_limit,
                ):
                    dangling_nodes.append(node)

            if not dangling_nodes:
                break

            if self.graph.number_of_nodes() < 4:
                logger.warning("Graph too small (<4 nodes), stopping pruning")
                break

            edges_to_remove = len({
                tuple(sorted((node, next(iter(self.graph.neighbors(node))))))
                for node in dangling_nodes
                if self.graph.degree(node) == 1
            })
            projected_pruned = total_pruned + edges_to_remove
            projected_percent = (
                projected_pruned / initial_edges * 100
                if initial_edges > 0 else 0
            )
            if projected_pruned > max_prunable_edges:
                logger.warning(
                    f"Stopping pruning at projected {projected_percent:.1f}% edge removal "
                    f"(limit {max_prune_percent:.1f}% / {max_prunable_edges} edges)"
                )
                break

            total_pruned = projected_pruned

            self.graph.remove_nodes_from(dangling_nodes)

            iteration += 1

            if iteration % 100 == 0:
                logger.debug(f"Pruning iteration {iteration}: {len(dangling_nodes)} nodes removed")

        removed_small_components, removed_component_edges = self._prune_small_disconnected_components()

        # Calculate metrics
        final_edges = self.graph.number_of_edges()
        final_nodes = self.graph.number_of_nodes()

        pruned_edges = initial_edges - final_edges
        pruned_percent = (pruned_edges / initial_edges * 100) if initial_edges > 0 else 0

        if pruned_percent > max_prune_percent:
            logger.warning(
                f"Aggressive pruning: {pruned_percent:.1f}% edges removed. "
                f"This may indicate over-pruning or legitimate geometry removal."
            )

        logger.info(
            f"Pruning complete: {iteration} iterations, "
            f"{pruned_edges} edges removed ({pruned_percent:.1f}%)"
        )
        if removed_small_components:
            logger.info(
                "Removed %d disconnected small components (%d edges)",
                removed_small_components,
                removed_component_edges,
            )

        # Graph metrics
        components = nx.number_connected_components(self.graph)
        max_degree = max(dict(self.graph.degree()).values()) if self.graph.number_of_nodes() > 0 else 0

        metrics = GraphMetrics(
            node_count=final_nodes,
            edge_count=final_edges,
            components=components,
            max_degree=max_degree,
            pruned_edges=pruned_edges,
            pruned_percent=pruned_percent,
            removed_small_components=removed_small_components,
            removed_small_component_edges=removed_component_edges,
        )

        logger.info(
            f"Graph metrics: {metrics.components} components, "
            f"max degree {metrics.max_degree}"
        )

        return metrics

    def _prune_small_disconnected_components(self) -> Tuple[int, int]:
        """
        Remove tiny components far from the graph centroid.

        This mirrors the old segment-level isolated-detail cleanup, but runs on
        the already-snapped graph where connected components are cheap to inspect.
        """
        if self.graph is None or self.graph.number_of_edges() == 0:
            return 0, 0

        total_edges = self.graph.number_of_edges()
        if total_edges <= 1:
            return 0, 0

        diagonal = math.hypot(
            self.bbox["maxX"] - self.bbox["minX"],
            self.bbox["maxY"] - self.bbox["minY"],
        )
        max_component_edges = max(3, int(total_edges * 0.08))

        edge_midpoints = np.array([
            ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
            for start, end in self.graph.edges
        ])
        drawing_centroid = edge_midpoints.mean(axis=0)

        nodes_to_remove: Set[Tuple[float, float]] = set()
        removed_components = 0
        removed_edges = 0

        for component_nodes in nx.connected_components(self.graph):
            subgraph = self.graph.subgraph(component_nodes)
            component_edges = list(subgraph.edges)
            if not component_edges:
                continue
            if len(component_edges) > max_component_edges:
                continue

            component_midpoints = np.array([
                ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
                for start, end in component_edges
            ])
            component_centroid = component_midpoints.mean(axis=0)
            distance = float(np.linalg.norm(component_centroid - drawing_centroid))
            if distance <= diagonal * 0.45:
                continue

            nodes_to_remove.update(component_nodes)
            removed_components += 1
            removed_edges += len(component_edges)

        if not nodes_to_remove or removed_edges >= total_edges:
            return 0, 0

        self.graph.remove_nodes_from(nodes_to_remove)
        return removed_components, removed_edges

    def get_active_segments(self, segments: List[Segment]) -> List[Segment]:
        """
        Return the segments whose edges still exist in the current graph.

        This lets downstream stages consume the pruned graph structure rather
        than the original snapped segment list.
        """
        if self.graph is None:
            raise ValueError("Graph not built. Call build_graph_and_snap first.")

        if not segments:
            return []

        active_indices = {
            data["index"]
            for _, _, data in self.graph.edges(data=True)
            if data.get("index") is not None
        }
        return [segment for index, segment in enumerate(segments) if index in active_indices]

    def _calculate_spur_length_limit(self) -> float:
        """
        Compute a conservative maximum length for removable dangling spurs.
        """
        if self.graph is None or self.graph.number_of_edges() == 0:
            return self.tolerance * 5 if self.tolerance else 0.0

        edge_lengths = [
            data.get('length', 0.0)
            for _, _, data in self.graph.edges(data=True)
        ]
        avg_length = sum(edge_lengths) / len(edge_lengths) if edge_lengths else 0.0

        return max(self.tolerance * 5, avg_length * 0.1)

    def _is_terminal_branch_candidate(
        self,
        node: Tuple[float, float],
        attachment: Tuple[float, float],
        branch_length: float,
        length_limit: float,
    ) -> bool:
        """
        Detect a dead-end branch that meets a longer run as a T-junction.

        This catches thin protrusions that are longer than the generic short-spur
        limit but still look like non-boundary branches.
        """
        if self.graph is None:
            return False
        if branch_length > length_limit:
            return False
        if self.graph.degree(attachment) < 3:
            return False

        branch_angle = self._edge_angle(node, attachment)
        if branch_angle is None:
            return False

        neighbor_edges = []
        for other in self.graph.neighbors(attachment):
            if other == node:
                continue
            angle = self._edge_angle(attachment, other)
            edge_data = self.graph.get_edge_data(attachment, other) or {}
            if angle is None:
                continue
            neighbor_edges.append((angle, edge_data.get("length", 0.0)))

        if len(neighbor_edges) < 2:
            return False

        for index, (left_angle, left_length) in enumerate(neighbor_edges):
            for right_angle, right_length in neighbor_edges[index + 1:]:
                if not self._is_parallel(left_angle, right_angle):
                    continue
                if not self._is_orthogonal(branch_angle, left_angle):
                    continue
                if min(left_length, right_length) < branch_length * 0.75:
                    continue
                return True

        return False

    def _calculate_adaptive_tolerance(self, segments: List[Segment]) -> float:
        """
        Calculate adaptive tolerance based on global scale and local segment lengths.

        Formula: min(global_tolerance, local_tolerance)
        - global_tolerance = bbox_diagonal * global_percent
        - local_tolerance = avg_segment_length * local_percent
        - Clamped to [min_tolerance_mm, max_tolerance_mm]

        Args:
            segments: List of segments

        Returns:
            Adaptive tolerance value
        """
        # Calculate global scale (bbox diagonal)
        bbox_width = self.bbox['maxX'] - self.bbox['minX']
        bbox_height = self.bbox['maxY'] - self.bbox['minY']
        bbox_diagonal = math.sqrt(bbox_width ** 2 + bbox_height ** 2)

        # Calculate local scale (average segment length)
        total_length = sum(seg.length() for seg in segments)
        avg_segment_length = total_length / len(segments) if segments else 0

        # Global tolerance: percentage of bbox diagonal
        global_tolerance = bbox_diagonal * self.adaptive_params['tolerance_global_percent'] / 100

        # Local tolerance: percentage of average segment length
        local_tolerance = avg_segment_length * self.adaptive_params['tolerance_local_percent'] / 100

        # Use minimum of global and local
        tolerance = min(global_tolerance, local_tolerance)

        # Add safety margin for floating-point precision (1.0001x)
        tolerance *= 1.0001

        # Clamp to [min, max] range
        min_tol = self.adaptive_params['min_tolerance_mm']
        max_tol = self.adaptive_params['max_tolerance_mm']
        tolerance = max(min_tol, min(max_tol, tolerance))

        return tolerance

    def _extend_dangling_endpoints(self, segments: List[Segment]) -> List[Segment]:
        if not segments:
            return []

        extension_length, estimate_method = self._estimate_endpoint_extension_length(segments)
        self.extension_metadata = {
            "attempted": extension_length > 0,
            "applied_count": 0,
            "extension_length": extension_length,
            "estimate_method": estimate_method,
            "skipped_reason": "",
            "applied_extensions": [],
        }
        if extension_length <= 0:
            return segments

        working_segments = list(segments)
        if len(working_segments) > int(self.adaptive_params["max_extension_segments"]):
            self.extension_metadata["attempted"] = False
            self.extension_metadata["skipped_reason"] = "segment_count_exceeded"
            return working_segments

        initial_graph = self._build_graph(working_segments)
        dangling_nodes = [node for node in initial_graph.nodes if initial_graph.degree(node) == 1]
        if len(dangling_nodes) > int(self.adaptive_params["max_extension_dangling_nodes"]):
            self.extension_metadata["attempted"] = False
            self.extension_metadata["skipped_reason"] = "dangling_node_count_exceeded"
            return working_segments

        max_extension_passes = int(self.adaptive_params.get("max_extension_passes", 4))

        for pass_index in range(max_extension_passes):
            graph = initial_graph if pass_index == 0 else self._build_graph(working_segments)
            extension_index = self._build_extension_index(graph, working_segments, extension_length)
            candidates = []

            for node in extension_index["dangling_nodes"]:
                candidate = self._find_extension_candidate(
                    graph,
                    working_segments,
                    node,
                    extension_length,
                    extension_index,
                )
                if candidate is None:
                    continue

                candidates.append(candidate)

            if not candidates:
                break

            selected_candidates = self._select_batch_extension_candidates(candidates)
            if not selected_candidates:
                break

            working_segments, applied_details = self._apply_extension_batch(
                working_segments,
                selected_candidates,
            )
            self.extension_metadata["applied_count"] += len(applied_details)
            self.extension_metadata["applied_extensions"].extend(applied_details)

        return working_segments

    def _build_extension_index(
        self,
        graph: nx.Graph,
        segments: List[Segment],
        extension_length: float,
    ) -> Dict:
        nodes = list(graph.nodes)
        node_array = np.array(nodes, dtype=float) if nodes else np.empty((0, 2), dtype=float)
        endpoint_tree = KDTree(node_array) if KDTree is not None and len(node_array) else None
        endpoint_buckets = (
            self._build_spatial_buckets(nodes, extension_length)
            if endpoint_tree is None and nodes
            else None
        )

        segment_lines = [
            LineString([segment.start.to_2d(), segment.end.to_2d()])
            for segment in segments
        ]
        segment_tree = STRtree(segment_lines) if segment_lines else None

        corridor_padding = max(
            self.tolerance * 4 if self.tolerance else 1.0,
            extension_length * self.adaptive_params["corridor_padding_ratio"],
        )

        return {
            "nodes": nodes,
            "endpoint_tree": endpoint_tree,
            "endpoint_buckets": endpoint_buckets,
            "segment_tree": segment_tree,
            "segment_lines": segment_lines,
            "dangling_nodes": [node for node in nodes if graph.degree(node) == 1],
            "corridor_padding": corridor_padding,
        }

    def _estimate_endpoint_extension_length(self, segments: List[Segment]) -> Tuple[float, str]:
        wall_thickness, method = self._estimate_wall_thickness(segments)
        extension = wall_thickness * self.adaptive_params["endpoint_extension_ratio"]
        extension = max(extension, self.adaptive_params["min_extension_mm"])
        extension = min(extension, self.adaptive_params["max_extension_mm"])
        return extension, method

    def _estimate_wall_thickness(self, segments: List[Segment]) -> Tuple[float, str]:
        connector_lengths = self._estimate_from_orthogonal_connectors(segments)
        if connector_lengths:
            return self._clamp_wall_thickness(median(connector_lengths)), "orthogonal_connectors"

        parallel_distances = self._estimate_from_parallel_pairs(segments)
        if parallel_distances:
            return self._clamp_wall_thickness(median(parallel_distances)), "parallel_pairs"

        return self._estimate_from_bbox(segments), "bbox_fallback"

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
        filtered = [segment for segment in segments if segment.length() > self.adaptive_params["min_extension_mm"]]
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

    def _find_extension_candidate(
        self,
        graph: nx.Graph,
        segments: List[Segment],
        node: Tuple[float, float],
        extension_length: float,
        extension_index: Dict,
    ) -> Optional[Dict]:
        neighbor = next(iter(graph.neighbors(node)))
        edge_data = graph.get_edge_data(node, neighbor) or {}
        segment_index = edge_data.get("index")
        if segment_index is None:
            return None

        segment = segments[segment_index]
        endpoint_name = "start" if self._point_key(segment.start.to_2d()) == self._point_key(node) else "end"
        source_length = segment.length()
        min_source_length = extension_length * self.adaptive_params["min_source_length_factor"]
        if source_length < min_source_length:
            return None

        direction = self._extension_direction(segment, endpoint_name)
        if direction is None:
            return None

        best_candidate = self._find_endpoint_candidate(
            node,
            direction,
            extension_length,
            extension_index,
        )

        segment_candidate = self._find_segment_candidate(
            segments,
            node,
            segment_index,
            direction,
            extension_length,
            extension_index,
        )
        if segment_candidate and (best_candidate is None or segment_candidate["distance"] < best_candidate["distance"]):
            best_candidate = segment_candidate

        if best_candidate is None:
            return None

        best_candidate["source_segment_index"] = segment_index
        best_candidate["source_endpoint_name"] = endpoint_name
        return best_candidate

    def _find_endpoint_candidate(
        self,
        node: Tuple[float, float],
        direction: Tuple[float, float],
        extension_length: float,
        extension_index: Dict,
    ) -> Optional[Dict]:
        endpoint_tree = extension_index["endpoint_tree"]
        best_candidate = None
        candidate_indices = self._query_endpoint_neighbors(
            extension_index["nodes"],
            endpoint_tree,
            extension_index.get("endpoint_buckets"),
            node,
            extension_length,
        )

        for candidate_index in candidate_indices:
            other_node = extension_index["nodes"][candidate_index]
            if other_node == node:
                continue

            distance, alignment = self._ray_distance_to_point(node, direction, other_node)
            if distance is None or distance > extension_length:
                continue
            if alignment > self.adaptive_params["parallel_tolerance_deg"]:
                continue

            if best_candidate is None or distance < best_candidate["distance"]:
                best_candidate = {
                    "target_kind": "endpoint",
                    "target_point": other_node,
                    "distance": distance,
                }

        return best_candidate

    def _find_segment_candidate(
        self,
        segments: List[Segment],
        node: Tuple[float, float],
        source_segment_index: int,
        direction: Tuple[float, float],
        extension_length: float,
        extension_index: Dict,
    ) -> Optional[Dict]:
        segment_tree = extension_index["segment_tree"]
        if segment_tree is None:
            return None

        best_candidate = None
        corridor = self._extension_query_box(
            node,
            direction,
            extension_length,
            extension_index["corridor_padding"],
        )
        candidate_indices = segment_tree.query(corridor)

        for target_index in candidate_indices:
            if target_index == source_segment_index:
                continue

            target_segment = segments[int(target_index)]
            target_angle = self._segment_angle(target_segment)
            direction_angle = math.degrees(math.atan2(direction[1], direction[0]))
            if not (
                self._is_parallel(direction_angle, target_angle)
                or self._is_orthogonal(direction_angle, target_angle)
            ):
                continue

            intersection = self._ray_segment_intersection(
                node,
                direction,
                target_segment,
                extension_length,
            )
            if intersection is None:
                continue

            point, distance, split_target = intersection
            if best_candidate is None or distance < best_candidate["distance"]:
                best_candidate = {
                    "target_kind": "segment",
                    "target_point": point,
                    "target_segment_index": int(target_index),
                    "distance": distance,
                    "split_target": split_target,
                }

        return best_candidate

    def _select_batch_extension_candidates(self, candidates: List[Dict]) -> List[Dict]:
        selected = []
        used_sources = set()
        used_segment_indices = set()
        used_target_points = set()

        for candidate in sorted(candidates, key=lambda item: item["distance"]):
            source_key = (candidate["source_segment_index"], candidate["source_endpoint_name"])
            if source_key in used_sources:
                continue

            target_point_key = self._point_key(candidate["target_point"])
            if target_point_key in used_target_points:
                continue

            target_segment_index = candidate.get("target_segment_index")
            involved_indices = {candidate["source_segment_index"]}
            if target_segment_index is not None:
                involved_indices.add(target_segment_index)

            if involved_indices & used_segment_indices:
                continue

            selected.append(candidate)
            used_sources.add(source_key)
            used_target_points.add(target_point_key)
            used_segment_indices.update(involved_indices)

        return selected

    def _apply_extension_batch(self, segments: List[Segment], candidates: List[Dict]) -> Tuple[List[Segment], List[Dict]]:
        applied_details: List[Dict] = []
        source_updates: Dict[int, Segment] = {}
        target_splits: Dict[int, Tuple[Segment, Segment]] = {}

        for candidate in candidates:
            source_index = candidate["source_segment_index"]
            original_segment = segments[source_index]
            original_point = (
                original_segment.start.to_2d()
                if candidate["source_endpoint_name"] == "start"
                else original_segment.end.to_2d()
            )
            target_point = Point(x=candidate["target_point"][0], y=candidate["target_point"][1])

            if candidate["source_endpoint_name"] == "start":
                source_updates[source_index] = Segment(
                    start=target_point,
                    end=original_segment.end,
                    meta=original_segment.meta,
                )
            else:
                source_updates[source_index] = Segment(
                    start=original_segment.start,
                    end=target_point,
                    meta=original_segment.meta,
                )

            detail = {
                "source_segment_index": source_index,
                "source_endpoint": candidate["source_endpoint_name"],
                "from_point": [float(original_point[0]), float(original_point[1])],
                "to_point": [float(target_point.x), float(target_point.y)],
                "extension_mm": round(self._distance(original_point, (target_point.x, target_point.y)), 3),
                "target_kind": candidate["target_kind"],
            }

            if candidate["target_kind"] == "segment" and candidate.get("split_target"):
                target_index = candidate["target_segment_index"]
                target_segment = segments[target_index]
                target_splits[target_index] = (
                    Segment(target_segment.start, target_point, target_segment.meta),
                    Segment(target_point, target_segment.end, target_segment.meta),
                )
                detail["target_segment_index"] = target_index
            elif candidate["target_kind"] == "endpoint":
                detail["target_point_index"] = [
                    float(candidate["target_point"][0]),
                    float(candidate["target_point"][1]),
                ]

            applied_details.append(detail)

        updated_segments: List[Segment] = []
        for index, segment in enumerate(segments):
            if index in target_splits:
                first, second = target_splits[index]
                if first.length() > self.tolerance:
                    updated_segments.append(first)
                if second.length() > self.tolerance:
                    updated_segments.append(second)
                continue

            if index in source_updates:
                updated_segments.append(source_updates[index])
                continue

            updated_segments.append(segment)

        return updated_segments, applied_details

    def _apply_tolerance_snapping(self, segments: List[Segment]) -> List[Segment]:
        """
        Apply tolerance snapping using KD-tree + Union-Find.

        Args:
            segments: List of input segments

        Returns:
            List of segments with snapped endpoints
        """
        if not segments:
            return []

        # Collect all unique endpoints
        endpoints = []
        endpoint_to_segment = []

        for i, seg in enumerate(segments):
            endpoints.append((seg.start.x, seg.start.y))
            endpoint_to_segment.append((i, 'start'))

            endpoints.append((seg.end.x, seg.end.y))
            endpoint_to_segment.append((i, 'end'))

        # Convert to numpy array
        endpoints_array = np.array(endpoints)

        # Find pairs within tolerance
        pairs = self._find_endpoint_pairs_within_tolerance(endpoints, endpoints_array, self.tolerance)

        logger.debug(f"Found {len(pairs)} endpoint pairs within tolerance {self.tolerance:.6f}")

        # Union-Find to merge endpoints
        parent = list(range(len(endpoints)))

        def find(x):
            """Find with path compression."""
            if parent[x] != x:
                parent[x] = find(parent[x])
            return parent[x]

        def union(x, y):
            """Union two sets."""
            px, py = find(x), find(y)
            if px != py:
                parent[px] = py

        # Merge endpoints
        for i, j in pairs:
            union(i, j)

        # Calculate representative (centroid) for each group
        groups = {}
        for idx in range(len(endpoints)):
            root = find(idx)
            if root not in groups:
                groups[root] = []
            groups[root].append(endpoints[idx])

        representatives = {}
        for root, indices in groups.items():
            # Calculate centroid
            coords = np.array(indices)
            centroid = coords.mean(axis=0)
            representatives[root] = centroid

        # Create snapped segments
        snapped_segments = []

        for i, seg in enumerate(segments):
            # Find snapped start point
            start_idx = i * 2
            start_root = find(start_idx)
            snapped_start = representatives[start_root]

            # Find snapped end point
            end_idx = i * 2 + 1
            end_root = find(end_idx)
            snapped_end = representatives[end_root]

            # Create snapped segment
            snapped_seg = Segment(
                start=Point(x=snapped_start[0], y=snapped_start[1]),
                end=Point(x=snapped_end[0], y=snapped_end[1]),
                meta=seg.meta
            )

            snapped_segments.append(snapped_seg)

        return snapped_segments

    def _find_endpoint_pairs_within_tolerance(
        self,
        endpoints: List[Tuple[float, float]],
        endpoints_array: np.ndarray,
        tolerance: float,
    ) -> Set[Tuple[int, int]]:
        if KDTree is not None:
            kdtree = KDTree(endpoints_array)
            return set(kdtree.query_pairs(tolerance))

        buckets = self._build_spatial_buckets(endpoints, tolerance)
        pairs: Set[Tuple[int, int]] = set()

        for left_index, coord in enumerate(endpoints):
            for right_index in self._query_endpoint_neighbors(
                endpoints,
                tree=None,
                buckets=buckets,
                point=coord,
                radius=tolerance,
            ):
                if right_index <= left_index:
                    continue
                other = endpoints[right_index]
                if math.hypot(coord[0] - other[0], coord[1] - other[1]) > tolerance:
                    continue
                pairs.add((left_index, right_index))

        return pairs

    def _build_spatial_buckets(
        self,
        points: Iterable[Tuple[float, float]],
        radius: float,
    ) -> Dict[Tuple[int, int], List[int]]:
        cell_size = max(radius, 1.0)
        buckets: Dict[Tuple[int, int], List[int]] = {}

        for index, point in enumerate(points):
            cell = self._bucket_cell(point, cell_size)
            buckets.setdefault(cell, []).append(index)

        return buckets

    def _query_endpoint_neighbors(
        self,
        points: List[Tuple[float, float]],
        tree,
        buckets: Optional[Dict[Tuple[int, int], List[int]]],
        point: Tuple[float, float],
        radius: float,
    ) -> List[int]:
        if tree is not None:
            return list(tree.query_ball_point([point[0], point[1]], radius))
        if not buckets:
            return []

        cell_size = max(radius, 1.0)
        cell_x, cell_y = self._bucket_cell(point, cell_size)
        candidate_indices: List[int] = []

        for neighbor_cell_x in range(cell_x - 1, cell_x + 2):
            for neighbor_cell_y in range(cell_y - 1, cell_y + 2):
                candidate_indices.extend(buckets.get((neighbor_cell_x, neighbor_cell_y), []))

        return candidate_indices

    def _bucket_cell(self, point: Tuple[float, float], cell_size: float) -> Tuple[int, int]:
        return (
            math.floor(point[0] / cell_size),
            math.floor(point[1] / cell_size),
        )

    def _build_graph(self, segments: List[Segment]) -> nx.Graph:
        """
        Build networkx graph from segments.

        Args:
            segments: List of segments

        Returns:
            networkx.Graph with nodes at endpoints and edges as segments
        """
        graph = nx.Graph()

        # Add edges (segments) to graph
        # NetworkX automatically creates nodes from edge endpoints
        for i, seg in enumerate(segments):
            start_node = (seg.start.x, seg.start.y)
            end_node = (seg.end.x, seg.end.y)

            graph.add_edge(
                start_node,
                end_node,
                index=i,
                meta=seg.meta,
                length=seg.length(),
            )

        return graph

    def _extension_direction(
        self,
        segment: Segment,
        endpoint_name: str,
    ) -> Optional[Tuple[float, float]]:
        if endpoint_name == "start":
            dx = segment.start.x - segment.end.x
            dy = segment.start.y - segment.end.y
        else:
            dx = segment.end.x - segment.start.x
            dy = segment.end.y - segment.start.y

        length = math.hypot(dx, dy)
        if length <= 1e-9:
            return None
        return (dx / length, dy / length)

    def _ray_distance_to_point(
        self,
        origin: Tuple[float, float],
        direction: Tuple[float, float],
        point: Tuple[float, float],
    ) -> Tuple[Optional[float], Optional[float]]:
        vx = point[0] - origin[0]
        vy = point[1] - origin[1]
        distance = math.hypot(vx, vy)
        if distance <= max(self.tolerance, 1e-9):
            return None, None

        target_direction = (vx / distance, vy / distance)
        alignment = self._angle_between_vectors(direction, target_direction)
        forward = vx * direction[0] + vy * direction[1]
        if forward <= max(self.tolerance, 1e-9):
            return None, None
        return distance, alignment

    def _ray_segment_intersection(
        self,
        origin: Tuple[float, float],
        direction: Tuple[float, float],
        segment: Segment,
        max_distance: float,
    ) -> Optional[Tuple[Tuple[float, float], float, bool]]:
        p = np.array(origin, dtype=float)
        r = np.array(direction, dtype=float)
        q = np.array(segment.start.to_2d(), dtype=float)
        s = np.array([
            segment.end.x - segment.start.x,
            segment.end.y - segment.start.y,
        ], dtype=float)

        cross_rs = self._cross_2d(r, s)
        if abs(cross_rs) <= 1e-9:
            return None

        q_minus_p = q - p
        t = self._cross_2d(q_minus_p, s) / cross_rs
        u = self._cross_2d(q_minus_p, r) / cross_rs

        if t <= max(self.tolerance, 1e-9) or t > max_distance:
            return None
        if u < -1e-9 or u > 1.0 + 1e-9:
            return None

        point = (origin[0] + direction[0] * t, origin[1] + direction[1] * t)
        split_target = not (
            self._distance(point, segment.start.to_2d()) <= self.tolerance
            or self._distance(point, segment.end.to_2d()) <= self.tolerance
        )
        return point, float(t), split_target

    def _extension_query_box(
        self,
        origin: Tuple[float, float],
        direction: Tuple[float, float],
        extension_length: float,
        padding: float,
    ):
        end_x = origin[0] + direction[0] * extension_length
        end_y = origin[1] + direction[1] * extension_length
        min_x = min(origin[0], end_x) - padding
        min_y = min(origin[1], end_y) - padding
        max_x = max(origin[0], end_x) + padding
        max_y = max(origin[1], end_y) + padding
        return box(min_x, min_y, max_x, max_y)

    def _segment_angle(self, segment: Segment) -> float:
        return math.degrees(
            math.atan2(segment.end.y - segment.start.y, segment.end.x - segment.start.x)
        )

    def _unit_vector(self, segment: Segment) -> Tuple[float, float]:
        dx = segment.end.x - segment.start.x
        dy = segment.end.y - segment.start.y
        length = math.hypot(dx, dy)
        if length <= 1e-9:
            return (1.0, 0.0)
        return (dx / length, dy / length)

    def _segment_midpoint(self, segment: Segment) -> Tuple[float, float]:
        return (
            (segment.start.x + segment.end.x) * 0.5,
            (segment.start.y + segment.end.y) * 0.5,
        )

    def _project_interval(self, segment: Segment, axis: Tuple[float, float]) -> Tuple[float, float]:
        projections = [
            segment.start.x * axis[0] + segment.start.y * axis[1],
            segment.end.x * axis[0] + segment.end.y * axis[1],
        ]
        return min(projections), max(projections)

    def _interval_overlap(
        self,
        left: Tuple[float, float],
        right: Tuple[float, float],
    ) -> float:
        return max(0.0, min(left[1], right[1]) - max(left[0], right[0]))

    def _angle_between_vectors(
        self,
        left: Tuple[float, float],
        right: Tuple[float, float],
    ) -> float:
        dot = max(-1.0, min(1.0, left[0] * right[0] + left[1] * right[1]))
        return math.degrees(math.acos(dot))

    def _edge_angle(
        self,
        start: Tuple[float, float],
        end: Tuple[float, float],
    ) -> Optional[float]:
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        if abs(dx) <= 1e-9 and abs(dy) <= 1e-9:
            return None
        return math.degrees(math.atan2(dy, dx))

    def _angle_delta(self, left_deg: float, right_deg: float) -> float:
        delta = abs((left_deg - right_deg) % 180.0)
        return min(delta, 180.0 - delta)

    def _is_parallel(self, left_deg: float, right_deg: float) -> bool:
        return self._angle_delta(left_deg, right_deg) <= self.adaptive_params["parallel_tolerance_deg"]

    def _is_orthogonal(self, left_deg: float, right_deg: float) -> bool:
        return abs(self._angle_delta(left_deg, right_deg) - 90.0) <= self.adaptive_params["orthogonal_tolerance_deg"]

    def _is_plausible_wall_thickness(self, value: float) -> bool:
        return self.adaptive_params["min_extension_mm"] * 0.2 <= value <= self.adaptive_params["max_extension_mm"] * 2.5

    def _clamp_wall_thickness(self, value: float) -> float:
        return max(
            self.adaptive_params["min_extension_mm"],
            min(self.adaptive_params["max_extension_mm"], value),
        )

    def _point_key(self, point: Tuple[float, float]) -> Tuple[float, float]:
        return (round(point[0], 6), round(point[1], 6))

    def _distance(self, left: Tuple[float, float], right: Tuple[float, float]) -> float:
        return math.hypot(left[0] - right[0], left[1] - right[1])

    def _cross_2d(self, left: np.ndarray, right: np.ndarray) -> float:
        return float(left[0] * right[1] - left[1] * right[0])

    def validate_planarity(self) -> Tuple[bool, str]:
        """
        Check if graph is planar (no edge crossings).

        Returns:
            Tuple of (is_planar, message)
        """
        if self.graph is None:
            raise ValueError("Graph not built. Call build_graph_and_snap first.")

        try:
            is_planar, _ = nx.check_planarity(self.graph)
            return is_planar, "Graph is planar" if is_planar else "Graph is non-planar"
        except Exception as e:
            return False, f"Planarity check failed: {e}"

    def get_graph(self) -> nx.Graph:
        """Get the constructed graph."""
        return self.graph
