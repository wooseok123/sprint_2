"""
Validation and HATCH IoU Check (STEP 9)
Implements boundary validation with simplify, make_valid, and HATCH IoU verification
"""
import logging
from typing import List, Tuple, Dict, Optional
from dataclasses import dataclass

from shapely.geometry import Polygon
from shapely.ops import unary_union

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of boundary validation."""
    is_valid: bool
    exterior_coords: List[List[float]]
    interiors_coords: List[List[List[float]]]
    metadata: Dict


class BoundaryValidator:
    """
    Boundary validator for geometric correction and HATCH verification.
    Implements STEP 9: Simplify, make_valid, HATCH IoU check
    """

    def __init__(self, simplify_tolerance: float = 0.001):
        """
        Initialize validator.

        Args:
            simplify_tolerance: Tolerance for simplify() operation
        """
        self.simplify_tolerance = simplify_tolerance

    def validate_and_correct(
        self,
        polygon: Polygon,
        hatch_boundaries: Optional[List] = None
    ) -> ValidationResult:
        """
        Validate and correct boundary polygon.

        Args:
            polygon: Extracted boundary polygon
            hatch_boundaries: Optional HATCH entity boundaries for IoU verification

        Returns:
            ValidationResult with corrected coordinates and metadata
        """
        if polygon is None:
            return ValidationResult(
                is_valid=False,
                exterior_coords=[],
                interiors_coords=[],
                metadata={'error': 'No polygon provided'}
            )

        logger.info("Validating and correcting boundary...")

        # Step 1: Simplify (remove collinear points)
        simplified = self._simplify_polygon(polygon)

        # Step 2: Make valid (fix topology)
        corrected = self._make_valid(simplified)

        # Step 3: Validate result
        if not corrected.is_valid or corrected.is_empty:
            logger.error("Failed to produce valid polygon")
            return ValidationResult(
                is_valid=False,
                exterior_coords=[],
                interiors_coords=[],
                metadata={
                    'error': 'Failed to produce valid polygon',
                    'simplified_area': simplified.area,
                    'corrected_area': corrected.area if corrected else 0
                }
            )

        # Step 4: HATCH IoU verification (if HATCH available)
        iou_confidence = self._calculate_hatch_iou(corrected, hatch_boundaries)

        # Extract coordinates
        exterior_coords = self._extract_exterior_coords(corrected)
        interiors_coords = self._extract_interiors_coords(corrected)

        # Calculate metadata
        metadata = {
            'area': corrected.area,
            'vertex_count': len(exterior_coords),
            'interior_count': len(interiors_coords),
            'confidence': iou_confidence,
            'simplified': simplified.area != corrected.area,
            'made_valid': not polygon.is_valid and corrected.is_valid
        }

        logger.info(
            f"Validation complete: area={metadata['area']:.2f}, "
            f"vertices={metadata['vertex_count']}, confidence={iou_confidence:.2f}"
        )

        return ValidationResult(
            is_valid=True,
            exterior_coords=exterior_coords,
            interiors_coords=interiors_coords,
            metadata=metadata
        )

    def _simplify_polygon(self, polygon: Polygon) -> Polygon:
        """
        Simplify polygon to remove redundant vertices.

        Args:
            polygon: Input polygon

        Returns:
            Simplified polygon
        """
        try:
            simplified = polygon.simplify(
                tolerance=self.simplify_tolerance,
                preserve_topology=True
            )

            vertex_reduction = (
                len(polygon.exterior.coords) - len(simplified.exterior.coords)
            )

            if vertex_reduction > 0:
                logger.debug(
                    f"Simplified: removed {vertex_reduction} vertices "
                    f"({len(polygon.exterior.coords)} → {len(simplified.exterior.coords)})"
                )

            return simplified

        except Exception as e:
            logger.warning(f"Simplify failed: {e}")
            return polygon

    def _make_valid(self, polygon: Polygon) -> Polygon:
        """
        Fix polygon topology using make_valid().

        Args:
            polygon: Input polygon

        Returns:
            Valid polygon
        """
        if polygon.is_valid:
            return polygon

        logger.info("Polygon invalid, applying make_valid()...")

        try:
            # Try shapely 2.0+ make_valid()
            if hasattr(polygon, 'make_valid'):
                valid = polygon.make_valid()

                # make_valid() may return MultiPolygon
                if valid.geom_type == 'MultiPolygon':
                    # Return largest polygon
                    valid = max(valid.geoms, key=lambda p: p.area)
                    logger.warning("make_valid() produced MultiPolygon, using largest")

                return valid
            else:
                # Fallback: buffer(0)
                logger.warning("make_valid() not available, using buffer(0) fallback")
                valid = polygon.buffer(0)

                if valid.geom_type == 'MultiPolygon':
                    valid = max(valid.geoms, key=lambda p: p.area)

                return valid

        except Exception as e:
            logger.error(f"make_valid() failed: {e}")
            # Return original polygon
            return polygon

    def _calculate_hatch_iou(
        self,
        polygon: Polygon,
        hatch_boundaries: Optional[List] = None
    ) -> float:
        """
        Calculate IoU (Intersection over Union) with HATCH boundaries.

        Args:
            polygon: Validated boundary polygon
            hatch_boundaries: Optional list of HATCH boundary segments/points

        Returns:
            IoU confidence score (0-1), or 1.0 if no HATCH available
        """
        if hatch_boundaries is None or len(hatch_boundaries) == 0:
            # No HATCH entities → assume valid
            return 1.0

        try:
            # Create polygon from HATCH boundaries
            # This is a simplified approach - actual HATCH processing would be more complex
            from shapely.geometry import MultiPoint

            # Collect all HATCH boundary points
            hatch_points = []
            for boundary in hatch_boundaries:
                if hasattr(boundary, 'segments'):
                    for seg in boundary.segments:
                        hatch_points.append((seg.start.x, seg.start.y))
                        hatch_points.append((seg.end.x, seg.end.y))
                elif hasattr(boundary, 'start') and hasattr(boundary, 'end'):
                    hatch_points.append((boundary.start.x, boundary.start.y))
                    hatch_points.append((boundary.end.x, boundary.end.y))
                elif isinstance(boundary, tuple) and len(boundary) == 2:
                    # (x, y) point
                    hatch_points.append(boundary)

            if not hatch_points:
                return 1.0

            # Create HATCH polygon (convex hull as approximation)
            hatch_multi_point = MultiPoint(hatch_points)

            if hatch_multi_point.is_empty:
                return 1.0

            hatch_polygon = hatch_multi_point.convex_hull

            if hatch_polygon.is_empty:
                return 1.0

            # Calculate IoU
            intersection = polygon.intersection(hatch_polygon)
            union = polygon.union(hatch_polygon)

            if union.is_empty or union.area == 0:
                return 0.0

            iou = intersection.area / union.area

            logger.info(f"HATCH IoU: {iou:.3f}")

            # Warn if low IoU
            if iou < 0.5:
                logger.warning(
                    f"Low HATCH IoU ({iou:.3f}) - detected boundary may not match HATCH entity"
                )

            return iou

        except Exception as e:
            logger.warning(f"Failed to calculate HATCH IoU: {e}")
            return 1.0  # Assume valid if calculation fails

    def _extract_exterior_coords(self, polygon: Polygon) -> List[List[float]]:
        """Extract exterior boundary coordinates."""
        coords = []
        for x, y in polygon.exterior.coords:
            coords.append([float(x), float(y)])
        return coords

    def _extract_interiors_coords(self, polygon: Polygon) -> List[List[List[float]]]:
        """Extract interior boundary coordinates (holes)."""
        interiors = []

        for interior in polygon.interiors:
            ring = []
            for x, y in interior.coords:
                ring.append([float(x), float(y)])
            interiors.append(ring)

        return interiors

    def calculate_sanity_checks(
        self,
        polygon: Polygon,
        bbox_area: float
    ) -> Dict[str, bool]:
        """
        Calculate sanity checks for validation result.

        Checks:
        - Area ratio (polygon area / bbox area) should be > 0.1
        - Vertex count should be < 500
        - Convex hull ratio should be reasonable

        Args:
            polygon: Validated polygon
            bbox_area: Bounding box area

        Returns:
            Dict of check results
        """
        checks = {}

        # Area ratio check
        if bbox_area > 0:
            area_ratio = polygon.area / bbox_area
            checks['area_ratio_ok'] = area_ratio > 0.1
            checks['area_ratio'] = area_ratio
        else:
            checks['area_ratio_ok'] = False
            checks['area_ratio'] = 0.0

        # Vertex count check
        vertex_count = len(polygon.exterior.coords)
        checks['vertex_count_ok'] = vertex_count < 500
        checks['vertex_count'] = vertex_count

        # Convex hull ratio check
        try:
            convex_hull = polygon.convex_hull
            if convex_hull.is_empty or convex_hull.area == 0:
                checks['convex_ratio_ok'] = False
                checks['convex_ratio'] = 0.0
            else:
                convex_ratio = polygon.area / convex_hull.area
                checks['convex_ratio_ok'] = convex_ratio > 0.5
                checks['convex_ratio'] = convex_ratio
        except Exception:
            checks['convex_ratio_ok'] = False
            checks['convex_ratio'] = 0.0

        return checks

    def should_invoke_ai_judge(self, validation_result: ValidationResult) -> bool:
        """
        Determine if AI judgment is needed for validation.

        Trigger conditions (any true):
        - Area ratio < 0.1 (too small)
        - Vertex count > 500 (too complex)
        - Confidence < 0.5 (low HATCH IoU)

        Args:
            validation_result: ValidationResult from validate_and_correct()

        Returns:
            True if AI judgment recommended
        """
        metadata = validation_result.metadata

        # Area ratio check (need bbox_area)
        if 'area_ratio' in metadata and metadata['area_ratio'] < 0.1:
            logger.warning("Area ratio too low, AI judgment recommended")
            return True

        # Vertex count check
        if metadata.get('vertex_count', 0) > 500:
            logger.warning("Vertex count too high, AI judgment recommended")
            return True

        # Confidence check
        if metadata.get('confidence', 1.0) < 0.5:
            logger.warning("Low confidence (HATCH IoU), AI judgment recommended")
            return True

        return False
