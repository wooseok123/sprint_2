"""
Unary Union and Boundary Extraction (STEP 8)
Implements shapely unary_union to merge polygons and extract exterior boundary
"""
import logging
from typing import List, Tuple, Optional

from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union

logger = logging.getLogger(__name__)


class BoundaryExtractor:
    """
    Boundary extractor for merging polygons and extracting exterior.
    Implements STEP 8: Unary union and exterior/interior extraction
    """

    def extract_boundary(self, polygons: List[Polygon]) -> Tuple[Optional[Polygon], dict]:
        """
        Merge polygons and extract exterior boundary.

        Args:
            polygons: List of valid shapely Polygons

        Returns:
            Tuple of (merged_polygon, metadata)
            - merged_polygon: Unary union result (or None if failed)
            - metadata: Dict with extraction info
        """
        if not polygons:
            logger.warning("No polygons to extract boundary from")
            return None, {'error': 'No polygons provided'}

        logger.info(f"Merging {len(polygons)} polygons")

        try:
            # Apply unary_union to merge all polygons
            merged = unary_union(polygons)

            # Handle result types
            if merged.is_empty:
                logger.error("Unary_union produced empty geometry")
                return None, {'error': 'Union produced empty geometry'}

            # Extract metadata
            metadata = {
                'input_count': len(polygons),
                'result_type': merged.geom_type,
                'is_valid': merged.is_valid,
                'area': merged.area
            }

            # Single building assumption → should be Polygon
            if isinstance(merged, Polygon):
                logger.info(f"Merged into single Polygon (area: {merged.area:.2f})")
                return merged, metadata

            elif isinstance(merged, MultiPolygon):
                # Multiple polygons (unexpected for single building)
                logger.warning(
                    f"Unary_union produced MultiPolygon with {len(merged.geoms)} polygons. "
                    f"This may indicate unresolved noise geometry or a boundary extraction mismatch."
                )

                # Return largest polygon
                largest = max(merged.geoms, key=lambda p: p.area)
                metadata['warning'] = f'MultiPolygon detected, using largest (area: {largest.area:.2f})'
                metadata['multi_count'] = len(merged.geoms)

                return largest, metadata

            else:
                # GeometryCollection or other
                logger.error(f"Unexpected geometry type: {merged.geom_type}")
                return None, {'error': f'Unexpected geometry type: {merged.geom_type}'}

        except Exception as e:
            logger.error(f"Error during unary_union: {e}")
            return None, {'error': str(e)}

    def get_coordinates(
        self,
        polygon: Polygon
    ) -> Tuple[List[List[float]], List[List[List[float]]]]:
        """
        Extract exterior and interior coordinates from polygon.

        Args:
            polygon: shapely Polygon

        Returns:
            Tuple of (exterior_coords, interiors_coords)
            - exterior_coords: [[x1, y1], [x2, y2], ...] (closed loop)
            - interiors_coords: [[[x1, y1], ...], ...] (list of interior rings)
        """
        if polygon is None:
            return [], []

        # Extract exterior coordinates
        exterior_coords = []
        for x, y in polygon.exterior.coords:
            exterior_coords.append([x, y])

        # Extract interior coordinates (holes/courtyards)
        interiors_coords = []

        for interior in polygon.interiors:
            interior_ring = []
            for x, y in interior.coords:
                interior_ring.append([x, y])
            interiors_coords.append(interior_ring)

        logger.info(
            f"Extracted boundary: exterior={len(exterior_coords)} points, "
            f"{len(interiors_coords)} interior rings"
        )

        return exterior_coords, interiors_coords

    def get_exterior_only(self, polygon: Polygon) -> List[List[float]]:
        """
        Get exterior boundary coordinates only.

        Args:
            polygon: shapely Polygon

        Returns:
            List of [x, y] coordinates forming exterior boundary
        """
        exterior, _ = self.get_coordinates(polygon)
        return exterior

    def get_interiors(self, polygon: Polygon) -> List[List[List[float]]]:
        """
        Get interior boundary coordinates (holes).

        Args:
            polygon: shapely Polygon

        Returns:
            List of interior rings, each as [[x, y], ...]
        """
        _, interiors = self.get_coordinates(polygon)
        return interiors

    def calculate_bbox_area(self, polygon: Polygon) -> float:
        """
        Calculate bounding box area of polygon.

        Args:
            polygon: shapely Polygon

        Returns:
            Bounding box area
        """
        if polygon is None:
            return 0.0

        bounds = polygon.bounds  # (minx, miny, maxx, maxy)
        width = bounds[2] - bounds[0]
        height = bounds[3] - bounds[1]

        return width * height

    def calculate_convex_hull_ratio(self, polygon: Polygon) -> float:
        """
        Calculate ratio of polygon area to convex hull area.
        Indicates "compactness" of shape.

        Args:
            polygon: shapely Polygon

        Returns:
            Ratio (0-1), where 1 = fully convex
        """
        if polygon is None:
            return 0.0

        try:
            convex_hull = polygon.convex_hull

            if convex_hull.is_empty or convex_hull.area == 0:
                return 0.0

            return polygon.area / convex_hull.area

        except Exception as e:
            logger.warning(f"Failed to calculate convex hull ratio: {e}")
            return 0.0
