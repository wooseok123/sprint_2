"""
Cycle Detection (STEP 6)
Uses shapely.polygonize() over snapped segments to recover closed faces.
"""
import logging
from typing import List, Tuple
from dataclasses import dataclass

from shapely.geometry import LineString, MultiLineString
from shapely.ops import polygonize

from core.parser import Segment

logger = logging.getLogger(__name__)


@dataclass
class DetectedCycle:
    """Detected cycle (polygon)."""
    coordinates: List[Tuple[float, float]]
    vertex_count: int
    area: float
    bbox_ratio: float  # Aspect ratio of bounding box


class CycleDetector:
    """
    Detect cycles from snapped segments using polygonize().

    This intentionally avoids relying on graph pruning so partially broken
    exterior linework does not collapse the remaining closed faces.
    """

    def __init__(self, segments: List[Segment]):
        self.segments = segments

    def detect_cycles(self) -> List[DetectedCycle]:
        """
        Detect closed cycles using shapely.polygonize().

        Returns:
            List of detected cycles
        """
        if not self.segments:
            logger.warning("No segments available for polygonize")
            return []

        lines = self._segments_to_linestrings()
        if not lines:
            logger.warning("No line strings to polygonize")
            return []

        try:
            polygons = list(polygonize(MultiLineString(lines)))
            logger.info(f"Detected {len(polygons)} polygons")
        except Exception as exc:
            logger.error(f"Error during polygonization: {exc}")
            return []

        cycles: List[DetectedCycle] = []
        for poly in polygons:
            if not poly.is_valid or poly.is_empty:
                continue

            coords = list(poly.exterior.coords)
            if coords and coords[0] == coords[-1]:
                coords = coords[:-1]
            if len(coords) < 3:
                continue

            min_x, min_y, max_x, max_y = poly.bounds
            bbox_height = max_y - min_y
            bbox_ratio = (max_x - min_x) / bbox_height if bbox_height > 0 else 1.0

            cycles.append(
                DetectedCycle(
                    coordinates=coords,
                    vertex_count=len(coords),
                    area=poly.area,
                    bbox_ratio=bbox_ratio,
                )
            )

        logger.info(f"Valid cycles detected: {len(cycles)}")
        return cycles

    def _segments_to_linestrings(self) -> List[LineString]:
        """Convert normalized segments to shapely LineString objects."""
        lines: List[LineString] = []
        for seg in self.segments:
            if seg.start.to_2d() == seg.end.to_2d():
                continue
            lines.append(LineString([seg.start.to_2d(), seg.end.to_2d()]))
        return lines

    def standardize_winding(self, cycles: List[DetectedCycle]) -> List[DetectedCycle]:
        """
        Standardize winding direction: CCW for exterior, CW for others.
        """
        if not cycles:
            return cycles

        sorted_cycles = sorted(cycles, key=lambda cycle: cycle.area, reverse=True)

        standardized = [
            DetectedCycle(
                coordinates=self._ensure_ccw(sorted_cycles[0].coordinates),
                vertex_count=sorted_cycles[0].vertex_count,
                area=sorted_cycles[0].area,
                bbox_ratio=sorted_cycles[0].bbox_ratio,
            )
        ]

        for cycle in sorted_cycles[1:]:
            standardized.append(
                DetectedCycle(
                    coordinates=self._ensure_cw(cycle.coordinates),
                    vertex_count=cycle.vertex_count,
                    area=cycle.area,
                    bbox_ratio=cycle.bbox_ratio,
                )
            )

        return standardized

    def _ensure_ccw(self, coords: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        area = 0.0
        for index, (x1, y1) in enumerate(coords):
            x2, y2 = coords[(index + 1) % len(coords)]
            area += (x2 - x1) * (y2 + y1)
        return coords if area < 0 else list(reversed(coords))

    def _ensure_cw(self, coords: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        area = 0.0
        for index, (x1, y1) in enumerate(coords):
            x2, y2 = coords[(index + 1) % len(coords)]
            area += (x2 - x1) * (y2 + y1)
        return coords if area > 0 else list(reversed(coords))
