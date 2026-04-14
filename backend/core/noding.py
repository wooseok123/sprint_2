"""
Noding with Shapely (STEP 3)
Implements automatic intersection splitting using shapely unary_union
"""
import logging
from typing import List
from shapely.geometry import LineString, MultiLineString

from core.parser import Point, Segment

logger = logging.getLogger(__name__)


class NodingProcessor:
    """
    Noding processor for splitting segments at intersection points.
    Implements STEP 3: Tolerance snapping pre-processing and noding
    """

    def __init__(self, tolerance: float = 0.001):
        """
        Initialize noding processor.

        Args:
            tolerance: Tolerance for geometric operations (in drawing units)
        """
        self.tolerance = tolerance

    def apply_noding(self, segments: List[Segment]) -> List[Segment]:
        """
        Apply shapely unary_union to split segments at intersections.

        Args:
            segments: List of input segments

        Returns:
            List of noded segments (split at intersections)
        """
        if not segments:
            logger.warning("No segments to perform noding on")
            return []

        logger.info(f"Applying noding to {len(segments)} segments")

        # Convert segments to shapely LineString objects
        line_strings = self._segments_to_linestrings(segments)
        logger.debug(f"Created {len(line_strings)} LineString objects")

        # Create MultiLineString
        multiline = MultiLineString(line_strings)

        # Apply unary_union to split at intersections
        # This automatically splits lines at T-junctions and X-junctions
        try:
            noded = multiline.union(multiline)  # unary_union

            # Extract noded geometries
            if noded.geom_type == 'MultiLineString':
                noded_lines = list(noded.geoms)
            elif noded.geom_type == 'LineString':
                noded_lines = [noded]
            else:
                # GeometryCollection or other - extract LineStrings
                noded_lines = [geom for geom in noded.geoms if geom.geom_type == 'LineString']

            logger.info(f"Noding produced {len(noded_lines)} segments (from {len(segments)} input)")

        except Exception as e:
            logger.error(f"Error during noding operation: {e}")
            # Fallback: return original segments
            return segments

        # Convert back to Segment format
        noded_segments = self._linestrings_to_segments(noded_lines)

        logger.info(f"Noding complete: {len(segments)} → {len(noded_segments)} segments")

        return noded_segments

    def _segments_to_linestrings(self, segments: List[Segment]) -> List[LineString]:
        """
        Convert Segment objects to shapely LineString objects.

        Args:
            segments: List of Segment objects

        Returns:
            List of LineString objects
        """
        line_strings = []

        for seg in segments:
            coords = [(seg.start.x, seg.start.y), (seg.end.x, seg.end.y)]

            try:
                line = LineString(coords)
                line_strings.append(line)
            except Exception as e:
                logger.warning(f"Failed to create LineString from segment: {e}")
                continue

        return line_strings

    def _linestrings_to_segments(
        self,
        line_strings: List[LineString]
    ) -> List[Segment]:
        """
        Convert shapely LineString objects back to Segment objects.
        Metadata remapping is intentionally skipped because downstream stages
        do not require source-level metadata for noded segments.

        Args:
            line_strings: List of noded LineString objects

        Returns:
            List of Segment objects
        """
        segments = []

        for line in line_strings:
            # Extract coordinates
            coords = list(line.coords)

            if len(coords) < 2:
                logger.debug("Skipping LineString with less than 2 points")
                continue

            # Get start and end points
            start_coord = coords[0]
            end_coord = coords[-1]

            segment = Segment(
                start=Point(x=start_coord[0], y=start_coord[1]),
                end=Point(x=end_coord[0], y=end_coord[1]),
                meta={'type': 'noded_segment'}
            )

            segments.append(segment)

        return segments
