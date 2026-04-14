"""
Area Filter (STEP 7)
Implements adaptive area filtering (0.5-2% of bbox area based on entity count and ARC density)
"""
import logging
from typing import List

from shapely.geometry import Polygon

from core.cycles import DetectedCycle

logger = logging.getLogger(__name__)


class AreaFilter:
    """
    Area filter for removing noise polygons.
    Implements STEP 7: Adaptive area filter (0.5-2% of bbox area)
    """

    def __init__(
        self,
        bbox: dict,
        entity_count: int = 0,
        adaptive_params: dict = None
    ):
        """
        Initialize area filter.

        Args:
            bbox: Bounding box {minX, minY, maxX, maxY}
            entity_count: Total entity count from DXF (for adaptive threshold)
            adaptive_params: Optional adaptive parameters
                {
                    'min_area_percent': 0.5,   # Minimum 0.5% of bbox
                    'max_area_percent': 2.0,   # Maximum 2% of bbox
                    'arc_density_factor': 0.1, # Adjustment for ARC density
                    'entity_count_factor': 0.0001  # Adjustment for entity count
                }
        """
        self.bbox = bbox
        self.entity_count = entity_count
        self.adaptive_params = adaptive_params or {
            'min_area_percent': 0.5,
            'max_area_percent': 2.0,
            'arc_density_factor': 0.1,
            'entity_count_factor': 0.0001
        }

        # Calculate bbox area
        self.bbox_area = (bbox['maxX'] - bbox['minX']) * (bbox['maxY'] - bbox['minY'])

    def filter_cycles(
        self,
        cycles: List[DetectedCycle],
        arc_density: float = 0.0
    ) -> List[Polygon]:
        """
        Filter cycles by adaptive area threshold.

        Args:
            cycles: List of detected cycles
            arc_density: Ratio of ARC entities (0-1) for adaptive adjustment

        Returns:
            List of valid shapely Polygons
        """
        if not cycles:
            logger.warning("No cycles to filter")
            return []

        logger.info(f"Filtering {len(cycles)} cycles by area")

        # Calculate adaptive threshold
        threshold = self._calculate_adaptive_threshold(arc_density)
        logger.info(f"Adaptive area threshold: {threshold:.2f} ({threshold/self.bbox_area*100:.2f}% of bbox)")

        # Filter and convert to Polygons
        valid_polygons = []

        for cycle in cycles:
            # Check validity
            try:
                poly = Polygon(cycle.coordinates)

                # Check if valid polygon
                if not poly.is_valid:
                    logger.debug(f"Skipping invalid polygon (self-intersection)")
                    continue

                # Check minimum vertices
                if len(poly.exterior.coords) < 4:  # 3 + closing point
                    logger.debug(f"Skipping polygon with <3 vertices")
                    continue

                # Check area threshold
                if poly.area < threshold:
                    logger.debug(
                        f"Filtering small polygon: area={poly.area:.2f} < threshold={threshold:.2f}"
                    )
                    continue

                # Valid polygon
                valid_polygons.append(poly)

            except Exception as e:
                logger.warning(f"Failed to create polygon from cycle: {e}")
                continue

        logger.info(f"Area filter: {len(cycles)} → {len(valid_polygons)} valid polygons")

        outer_candidates = self._get_outer_candidates(valid_polygons)
        logger.info(
            f"Containment filter: {len(valid_polygons)} → {len(outer_candidates)} outer candidates"
        )

        return outer_candidates

    def _calculate_adaptive_threshold(self, arc_density: float) -> float:
        """
        Calculate adaptive area threshold.

        Formula:
        base_threshold = bbox_area * [min_percent, max_percent]
        adjustment = entity_count * entity_factor + arc_density * arc_factor
        final_threshold = base_threshold * (1 + adjustment)

        Args:
            arc_density: Ratio of ARC entities (0-1)

        Returns:
            Adaptive threshold value
        """
        # Base threshold range
        min_threshold = self.bbox_area * self.adaptive_params['min_area_percent'] / 100
        max_threshold = self.bbox_area * self.adaptive_params['max_area_percent'] / 100

        # Calculate adaptive adjustments
        entity_adjustment = self.entity_count * self.adaptive_params['entity_count_factor']
        arc_adjustment = arc_density * self.adaptive_params['arc_density_factor']

        # Total adjustment
        adjustment = entity_adjustment + arc_adjustment

        # Apply adjustment to midpoint of range
        base_threshold = (min_threshold + max_threshold) / 2
        adjusted_threshold = base_threshold * (1 + adjustment)

        # Clamp to [min, max] range
        final_threshold = max(min_threshold, min(max_threshold, adjusted_threshold))

        return final_threshold

    def _get_outer_candidates(self, polygons: List[Polygon]) -> List[Polygon]:
        """
        Remove polygons that are fully covered by another polygon.
        """
        if len(polygons) < 2:
            return polygons

        outer_candidates: List[Polygon] = []
        for polygon in polygons:
            is_inside = False
            for other in polygons:
                if polygon is other or other.equals(polygon):
                    continue
                if other.covers(polygon):
                    is_inside = True
                    break

            if not is_inside:
                outer_candidates.append(polygon)

        return outer_candidates

    def calculate_area_statistics(self, cycles: List[DetectedCycle]) -> dict:
        """
        Calculate area statistics for cycles.

        Args:
            cycles: List of detected cycles

        Returns:
            Dict with area statistics
        """
        if not cycles:
            return {
                'count': 0,
                'total_area': 0,
                'avg_area': 0,
                'min_area': 0,
                'max_area': 0,
                'areas': []
            }

        areas = [cycle.area for cycle in cycles]

        return {
            'count': len(areas),
            'total_area': sum(areas),
            'avg_area': sum(areas) / len(areas) if areas else 0,
            'min_area': min(areas) if areas else 0,
            'max_area': max(areas) if areas else 0,
            'areas': areas
        }

    def should_invoke_ai_judge(self, area_stats: dict) -> bool:
        """
        Determine if AI judgment is needed for area filtering.

        Trigger condition: sorted_areas[0] / sorted_areas[1] < 5
        (1st and 2nd largest polygons are similar size)

        Args:
            area_stats: Area statistics from calculate_area_statistics

        Returns:
            True if AI judgment recommended
        """
        if area_stats['count'] < 2:
            return False

        # Sort areas descending
        sorted_areas = sorted(area_stats['areas'], reverse=True)

        # Check ratio of 1st to 2nd
        if sorted_areas[1] > 0:
            ratio = sorted_areas[0] / sorted_areas[1]

            if ratio < 5:
                logger.warning(
                    f"Ambiguous area distribution: 1st/2nd ratio = {ratio:.2f} < 5. "
                    f"AI judgment recommended."
                )
                return True

        return False
