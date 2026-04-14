"""
Graph Construction and Pruning (STEP 4-5)
Implements tolerance snapping with KD-tree + Union-Find, and dangling edge pruning
"""
import math
import logging
from typing import List, Dict, Tuple
from dataclasses import dataclass

import numpy as np
from scipy.spatial import KDTree
import networkx as nx

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
        self.adaptive_params = adaptive_params or {
            'tolerance_global_percent': 0.1,
            'tolerance_local_percent': 1.0,
            'min_tolerance_mm': 0.001,
            'max_tolerance_mm': 1.0
        }

        self.tolerance = None
        self.graph = None

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

        # Build networkx graph
        self.graph = self._build_graph(snapped_segments)

        logger.info(f"Graph built: {self.graph.number_of_nodes()} nodes, {self.graph.number_of_edges()} edges")

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
        spur_length_limit = self._calculate_spur_length_limit()

        while iteration < max_iterations:
            dangling_nodes = []
            for node in self.graph.nodes:
                if self.graph.degree(node) != 1:
                    continue

                neighbor = next(iter(self.graph.neighbors(node)))
                edge_data = self.graph.get_edge_data(node, neighbor) or {}
                if edge_data.get('length', float('inf')) <= spur_length_limit:
                    dangling_nodes.append(node)

            if not dangling_nodes:
                break

            if self.graph.number_of_nodes() < 4:
                logger.warning("Graph too small (<4 nodes), stopping pruning")
                break

            edges_to_remove = sum(self.graph.degree(n) for n in dangling_nodes)
            projected_pruned = total_pruned + edges_to_remove
            projected_percent = (
                projected_pruned / initial_edges * 100
                if initial_edges > 0 else 0
            )
            if projected_percent > max_prune_percent:
                logger.warning(
                    f"Stopping pruning at projected {projected_percent:.1f}% edge removal "
                    f"(limit {max_prune_percent:.1f}%)"
                )
                break

            total_pruned = projected_pruned

            self.graph.remove_nodes_from(dangling_nodes)

            iteration += 1

            if iteration % 100 == 0:
                logger.debug(f"Pruning iteration {iteration}: {len(dangling_nodes)} nodes removed")

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

        # Graph metrics
        components = nx.number_connected_components(self.graph)
        max_degree = max(dict(self.graph.degree()).values()) if self.graph.number_of_nodes() > 0 else 0

        metrics = GraphMetrics(
            node_count=final_nodes,
            edge_count=final_edges,
            components=components,
            max_degree=max_degree,
            pruned_edges=pruned_edges,
            pruned_percent=pruned_percent
        )

        logger.info(
            f"Graph metrics: {metrics.components} components, "
            f"max degree {metrics.max_degree}"
        )

        return metrics

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

        # Build KD-tree
        kdtree = KDTree(endpoints_array)

        # Find pairs within tolerance
        pairs = kdtree.query_pairs(self.tolerance)

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
            start_idx = endpoint_to_segment[i * 2][1] == 'start' and i * 2 or i * 2 + 1
            start_root = find(start_idx)
            snapped_start = representatives[start_root]

            # Find snapped end point
            end_idx = endpoint_to_segment[i * 2 + 1][1] == 'end' and i * 2 + 1 or i * 2
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
