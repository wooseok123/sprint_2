"""
DXF Parser and Segment Normalizer (STEP 1-2)
Implements ezdxf parsing, recursive block explosion, and segment normalization
"""
import math
import logging
from typing import List, Dict, Tuple, Any, Optional, Set
from dataclasses import dataclass

import ezdxf
from ezdxf.entities import DXFEntity
from ezdxf.math import Matrix44, Vec3

logger = logging.getLogger(__name__)


@dataclass
class Point:
    """2D Point with z-coordinate (optional, for future 3D support)."""
    x: float
    y: float
    z: float = 0.0

    def to_tuple(self) -> Tuple[float, float, float]:
        return (self.x, self.y, self.z)

    def to_2d(self) -> Tuple[float, float]:
        return (self.x, self.y)


@dataclass
class Segment:
    """Normalized segment representation."""
    start: Point
    end: Point
    meta: Dict[str, Any]

    def length(self) -> float:
        """Calculate segment length."""
        dx = self.end.x - self.start.x
        dy = self.end.y - self.start.y
        return math.sqrt(dx * dx + dy * dy)


@dataclass
class ParsedDXF:
    """Result of DXF parsing."""
    segments: List[Segment]
    hatch_entities: List[Segment]
    bbox: Dict[str, float]  # {minX, minY, maxX, maxY}
    units: str  # Detected units from $INSUNITS
    insunits_code: int
    unit_scale_to_mm: float
    entity_count: int


class DXFParser:
    """
    DXF Parser with recursive block explosion and segment normalization.
    Implements STEP 1: DXF parsing and block explosion
    Implements STEP 2: Segment normalization with ARC→LINE approximation
    """

    # Unit mappings from DWG $INSUNITS
    UNIT_NAMES = {
        0: "Unspecified", 1: "Inches", 2: "Feet", 3: "Miles",
        4: "Millimeters", 5: "Centimeters", 6: "Meters", 7: "Kilometers",
        8: "Microinches", 9: "Mils", 10: "Yards", 11: "Angstroms",
        12: "Nanometers", 13: "Microns", 14: "Decimeters", 15: "Decameters",
        16: "Hectometers", 17: "Gigameters", 18: "Astro", 19: "Lightyears",
        20: "Parsecs"
    }

    UNITS_TO_MM = {
        0: 1.0,
        1: 25.4,
        2: 304.8,
        4: 1.0,
        5: 10.0,
        6: 1000.0,
    }

    # Entities to collect (per Scope Boundaries)
    # NOTE: CIRCLE is EXCLUDED per plan (causes noise, rare for outer walls)
    TARGET_ENTITY_TYPES = {'LINE', 'LWPOLYLINE', 'POLYLINE', 'SPLINE', 'ARC', 'INSERT'}
    BLOCK_REFERENCE_TYPES = {'INSERT'}

    def __init__(self, filepath: str):
        """
        Initialize parser with DXF file path.

        Args:
            filepath: Path to DXF file
        """
        self.filepath = filepath
        self.doc: Optional[ezdxf.document.Drawing] = None
        self.insunits_code = 0
        self.unit_scale_to_mm = 1.0

    def parse(self) -> ParsedDXF:
        """
        Parse DXF file and normalize to segments.

        Returns:
            ParsedDXF with segments, HATCH entities, bbox, units, entity count
        """
        logger.info(f"Parsing DXF file: {self.filepath}")

        try:
            # Load DXF document
            self.doc = ezdxf.readfile(self.filepath)
            logger.info(f"DXF version: {self.doc.dxfversion}")

        except IOError as e:
            logger.error(f"Failed to read DXF file: {e}")
            raise
        except ezdxf.DXFStructureError as e:
            logger.error(f"Invalid DXF structure: {e}")
            raise

        # Normalize all downstream geometry to millimeters.
        self.insunits_code, units, self.unit_scale_to_mm = self._detect_units()
        logger.info(
            f"Detected units: {units} (INSUNITS={self.insunits_code}, "
            f"scale={self.unit_scale_to_mm} mm/unit)"
        )

        # Collect all segments
        segments = []
        hatch_entities = []

        # Process modelspace entities
        msp = self.doc.modelspace()
        entity_count = 0

        for entity in msp:
            if not self._is_entity_renderable(entity):
                continue

            entity_count += 1

            try:
                entity_segments, entity_hatches = self._collect_entity_geometry(entity)
                segments.extend(entity_segments)
                hatch_entities.extend(entity_hatches)
            except Exception as e:
                logger.warning(f"Failed to process entity {entity.dxftype()}: {e}")
                continue

        logger.info(f"Processed {entity_count} entities, extracted {len(segments)} segments")
        logger.info(f"Collected {len(hatch_entities)} HATCH entities for validation")

        # Calculate bounding box
        bbox_dict = self._calculate_bbox(segments)
        logger.info(f"Bounding box: {bbox_dict}")

        return ParsedDXF(
            segments=segments,
            hatch_entities=hatch_entities,
            bbox=bbox_dict,
            units=units,
            insunits_code=self.insunits_code,
            unit_scale_to_mm=self.unit_scale_to_mm,
            entity_count=entity_count
        )

    def _detect_units(self) -> Tuple[int, str, float]:
        """
        Detect drawing units from $INSUNITS.

        Returns:
            Tuple of (INSUNITS code, unit name, scale-to-mm)
        """
        try:
            insunits = self.doc.header.get('$INSUNITS', 0)
            return (
                insunits,
                self.UNIT_NAMES.get(insunits, "Unknown"),
                self.UNITS_TO_MM.get(insunits, 1.0),
            )
        except Exception:
            return 0, "Unknown", 1.0

    def _collect_entity_geometry(
        self,
        entity: DXFEntity,
        transform: Optional[Matrix44] = None,
        visited: Optional[Set[str]] = None,
    ) -> Tuple[List[Segment], List[Segment]]:
        """
        Collect normalized boundary segments and HATCH validation segments.
        """
        dxftype = entity.dxftype()
        visited = visited or set()

        if not self._is_entity_renderable(entity):
            return [], []

        if dxftype == 'HATCH':
            return [], self._process_hatch(entity, transform)

        if dxftype in self.BLOCK_REFERENCE_TYPES:
            return self._process_insert(entity, visited=visited, parent_transform=transform)

        if dxftype in self.TARGET_ENTITY_TYPES:
            return self._process_entity(entity, transform), []

        return [], []

    def _is_entity_renderable(self, entity: DXFEntity) -> bool:
        """
        Apply the same visibility rules used by the frontend DXF viewer.

        This prevents backend geometry extraction from including entities that
        AutoCAD/dxf-viewer would not render, such as entities on off/frozen
        layers or entities marked invisible.
        """
        if bool(getattr(entity.dxf, 'invisible', 0)):
            return False

        if bool(getattr(entity.dxf, 'paperspace', 0)):
            return False

        layer_name = getattr(entity.dxf, 'layer', None)
        return self._is_layer_renderable(layer_name)

    def _is_layer_renderable(self, layer_name: Optional[str]) -> bool:
        """
        Match dxf-viewer's layer visibility behavior.

        Layer "0" is treated specially for block content, so we do not suppress
        it here based on layer flags alone.
        """
        if self.doc is None or layer_name in (None, '', '0'):
            return True

        try:
            layer = self.doc.layers.get(layer_name)
        except Exception:
            logger.debug("Layer definition not found for '%s'; treating as visible", layer_name)
            return True

        color = getattr(layer.dxf, 'color', 7)
        flags = getattr(layer.dxf, 'flags', 0)

        is_off = color < 0
        is_frozen = (flags & 1) != 0 or (flags & 2) != 0
        return not is_off and not is_frozen

    def _process_insert(
        self,
        insert_entity: DXFEntity,
        visited: Set[str],
        parent_transform: Optional[Matrix44] = None
    ) -> Tuple[List[Segment], List[Segment]]:
        """
        Recursively process INSERT entity (block reference).

        Args:
            insert_entity: INSERT entity to process
            visited: Set of visited block names to prevent cycles

        Returns:
            List of segments from exploded block
        """
        block_name = insert_entity.dxf.name

        # Prevent infinite recursion
        if block_name in visited:
            logger.warning(f"Circular block reference detected: {block_name}")
            return [], []

        # Compose block transform with any parent insert transform so nested INSERTs
        # are expanded in world coordinates rather than their local block space.
        insert_transform = self._build_insert_transform(insert_entity)
        combined_transform = self._combine_transforms(parent_transform, insert_transform)

        # Get block definition
        try:
            block_def = self.doc.blocks.get(block_name)
        except ValueError:
            logger.warning(f"Block definition not found: {block_name}")
            return [], []

        # Mark as visited
        new_visited = visited.copy()
        new_visited.add(block_name)

        segments = []
        hatch_segments = []

        # Process entities in block
        for entity in block_def:
            entity_segments, entity_hatches = self._collect_entity_geometry(
                entity,
                transform=combined_transform,
                visited=new_visited,
            )
            segments.extend(entity_segments)
            hatch_segments.extend(entity_hatches)

        return segments, hatch_segments

    def _process_entity(self, entity: DXFEntity, transform: Optional[Matrix44] = None) -> List[Segment]:
        """
        Process single entity and convert to normalized segments.

        Args:
            entity: DXF entity to process
            transform: Optional cumulative transform matrix

        Returns:
            List of normalized segments
        """
        dxftype = entity.dxftype()

        if dxftype == 'LINE':
            return self._process_line(entity, transform)
        elif dxftype == 'LWPOLYLINE':
            return self._process_lwpolyline(entity, transform)
        elif dxftype == 'POLYLINE':
            return self._process_polyline(entity, transform)
        elif dxftype == 'SPLINE':
            return self._process_spline(entity, transform)
        elif dxftype == 'ARC':
            return self._process_arc(entity, transform)
        elif dxftype == 'HATCH':
            # Store HATCH as boundary segments for validation
            return self._process_hatch(entity, transform)
        else:
            logger.warning(f"Unsupported entity type: {dxftype}")
            return []

    def _build_insert_transform(self, insert_entity: DXFEntity) -> Matrix44:
        """
        Build a 2D affine transform matrix for an INSERT entity.

        The matrix maps points from the referenced block's local coordinate
        system into the parent coordinate system.
        """
        insertion = insert_entity.dxf.insert
        xscale = insert_entity.dxf.xscale if hasattr(insert_entity.dxf, 'xscale') else 1.0
        yscale = insert_entity.dxf.yscale if hasattr(insert_entity.dxf, 'yscale') else 1.0
        rotation = insert_entity.dxf.rotation if hasattr(insert_entity.dxf, 'rotation') else 0.0
        rad = math.radians(rotation)

        return Matrix44.chain(
            Matrix44.scale(xscale, yscale, 1.0),
            Matrix44.z_rotate(rad),
            Matrix44.translate(insertion.x, insertion.y, insertion.z),
        )

    def _combine_transforms(
        self,
        parent_transform: Optional[Matrix44],
        child_transform: Matrix44
    ) -> Matrix44:
        """Compose parent and child affine transforms."""
        if parent_transform is None:
            return child_transform
        return Matrix44.chain(child_transform, parent_transform)

    def _apply_transform(
        self,
        point: Tuple[float, float],
        transform: Optional[Matrix44]
    ) -> Point:
        """
        Apply the cumulative transform and normalize the result to millimeters.

        Args:
            point: (x, y) coordinate
            transform: Optional affine transform matrix

        Returns:
            Transformed Point
        """
        vertex = Vec3(point[0], point[1], 0.0)
        if transform is not None:
            vertex = transform.transform(vertex)

        return Point(
            x=float(vertex.x) * self.unit_scale_to_mm,
            y=float(vertex.y) * self.unit_scale_to_mm,
            z=float(vertex.z) * self.unit_scale_to_mm,
        )

    def _process_line(self, line: DXFEntity, transform: Optional[Matrix44] = None) -> List[Segment]:
        """Process LINE entity."""
        start = self._apply_transform((line.dxf.start.x, line.dxf.start.y), transform)
        end = self._apply_transform((line.dxf.end.x, line.dxf.end.y), transform)

        return [Segment(
            start=start,
            end=end,
            meta={'type': 'line', 'layer': line.dxf.layer}
        )]

    def _process_lwpolyline(self, lwpoly: DXFEntity, transform: Optional[Matrix44] = None) -> List[Segment]:
        """
        Process LWPOLYLINE entity.
        Handles bulge values for arc segments.
        """
        points = list(lwpoly.vertices_in_wcs())
        segments = []

        for i in range(len(points) - 1):
            start = self._apply_transform(points[i], transform)
            end = self._apply_transform(points[i + 1], transform)

            # Check for bulge (arc segment)
            # Try multiple methods for different ezdxf versions
            bulge = 0
            try:
                if hasattr(lwpoly, 'get_bulge'):
                    bulge = lwpoly.get_bulge(i)
                elif hasattr(lwpoly.dxf, 'bulges') and lwpoly.dxf.bulges and i < len(lwpoly.dxf.bulges):
                    bulge = lwpoly.dxf.bulges[i]
            except (AttributeError, IndexError):
                bulge = 0

            if abs(bulge) < 1e-10:
                # Straight line segment
                segments.append(Segment(
                    start=start,
                    end=end,
                    meta={'type': 'line', 'layer': lwpoly.dxf.layer}
                ))
            else:
                # Arc segment - convert to line approximation
                arc_segments = self._bulge_to_arc_segments(start, end, bulge)
                segments.extend(arc_segments)

        # Handle closed polylines
        if lwpoly.closed:
            start = self._apply_transform(points[-1], transform)
            end = self._apply_transform(points[0], transform)
            bulge = 0
            try:
                if hasattr(lwpoly, 'get_bulge'):
                    bulge = lwpoly.get_bulge(len(points) - 1)
                elif hasattr(lwpoly.dxf, 'bulges') and lwpoly.dxf.bulges and len(points) - 1 < len(lwpoly.dxf.bulges):
                    bulge = lwpoly.dxf.bulges[len(points) - 1]
            except (AttributeError, IndexError):
                bulge = 0

            if abs(bulge) < 1e-10:
                segments.append(Segment(
                    start=start,
                    end=end,
                    meta={'type': 'line', 'layer': lwpoly.dxf.layer}
                ))
            else:
                arc_segments = self._bulge_to_arc_segments(start, end, bulge)
                segments.extend(arc_segments)

        return segments

    def _process_polyline(self, poly: DXFEntity, transform: Optional[Matrix44] = None) -> List[Segment]:
        """Process POLYLINE entity (2D only)."""
        points = []
        for vertex in poly.vertices:
            if hasattr(vertex.dxf, 'z') and abs(vertex.dxf.z) > 1e-10:
                logger.warning("3D POLYLINE detected, z-coordinate will be ignored")
            points.append((vertex.dxf.location.x, vertex.dxf.location.y))

        segments = []

        for i in range(len(points) - 1):
            start = self._apply_transform(points[i], transform)
            end = self._apply_transform(points[i + 1], transform)

            segments.append(Segment(
                start=start,
                end=end,
                meta={'type': 'line', 'layer': poly.dxf.layer}
            ))

        # Handle closed polylines
        if poly.is_closed:
            start = self._apply_transform(points[-1], transform)
            end = self._apply_transform(points[0], transform)
            segments.append(Segment(
                start=start,
                end=end,
                meta={'type': 'line', 'layer': poly.dxf.layer}
            ))

        return segments

    def _process_spline(self, spline: DXFEntity, transform: Optional[Matrix44] = None) -> List[Segment]:
        """
        Process SPLINE entity.
        Discretizes into LWPOLYLINE approximation (50 segments per plan).
        """
        try:
            # Discretize spline into polyline
            discretized = spline.discretize(segment_count=50)

            segments = []

            # Convert to line segments
            points = list(discretized.vertices_in_wcs())
            for i in range(len(points) - 1):
                start = self._apply_transform(points[i], transform)
                end = self._apply_transform(points[i + 1], transform)

                segments.append(Segment(
                    start=start,
                    end=end,
                    meta={'type': 'spline_segment', 'layer': spline.dxf.layer}
                ))

            return segments

        except Exception as e:
            logger.warning(f"Failed to discretize SPLINE: {e}")
            return []

    def _process_arc(self, arc: DXFEntity, transform: Optional[Matrix44] = None) -> List[Segment]:
        """
        Process ARC entity.
        Converts to LINE approximation (20 segments per plan).
        """
        start_angle = math.radians(arc.dxf.start_angle)
        end_angle = math.radians(arc.dxf.end_angle)

        # Number of segments for arc approximation
        num_segments = 20

        # Calculate angle step
        if arc.dxf.end_angle < arc.dxf.start_angle:
            # Handle wrap-around case
            total_angle = (360 + arc.dxf.end_angle - arc.dxf.start_angle) * math.pi / 180
        else:
            total_angle = (end_angle - start_angle)

        angle_step = total_angle / num_segments

        segments = []
        prev_point = None

        for i in range(num_segments + 1):
            angle = start_angle + i * angle_step
            x = arc.dxf.center.x + arc.dxf.radius * math.cos(angle)
            y = arc.dxf.center.y + arc.dxf.radius * math.sin(angle)
            curr_point = self._apply_transform((x, y), transform)

            if prev_point is not None:
                segments.append(Segment(
                    start=prev_point,
                    end=curr_point,
                    meta={
                        'type': 'arc_segment',
                        'layer': arc.dxf.layer,
                        'center': [
                            arc.dxf.center.x * self.unit_scale_to_mm,
                            arc.dxf.center.y * self.unit_scale_to_mm,
                        ],
                        'radius': arc.dxf.radius * self.unit_scale_to_mm,
                    }
                ))

            prev_point = curr_point

        return segments

    def _process_hatch(self, hatch: DXFEntity, transform: Optional[Matrix44] = None) -> List[Segment]:
        """
        Process HATCH entity.
        Extracts boundary loops as segments for validation (STEP 9).
        """
        segments = []

        try:
            # Get boundary paths
            for path in hatch.paths:
                if path.path_type_flags & 2:  # Polyline path
                    # Extract edges from polyline
                    for edge in path.edges:
                        if edge.edge_type == 'LineEdge':
                            start = self._apply_transform((edge.start[0], edge.start[1]), transform)
                            end = self._apply_transform((edge.end[0], edge.end[1]), transform)

                            segments.append(Segment(
                                start=start,
                                end=end,
                                meta={'type': 'hatch_boundary', 'layer': hatch.dxf.layer}
                            ))

                        elif edge.edge_type == 'ArcEdge':
                            # Convert arc to segments
                            start_angle = math.radians(edge.start_angle)
                            end_angle = math.radians(edge.end_angle)

                            num_segments = 20
                            angle_step = (end_angle - start_angle) / num_segments
                            prev_point = None

                            for i in range(num_segments + 1):
                                angle = start_angle + i * angle_step
                                x = edge.center[0] + edge.radius * math.cos(angle)
                                y = edge.center[1] + edge.radius * math.sin(angle)
                                curr_point = self._apply_transform((x, y), transform)

                                if prev_point is not None:
                                    segments.append(Segment(
                                        start=prev_point,
                                        end=curr_point,
                                        meta={'type': 'hatch_boundary', 'layer': hatch.dxf.layer}
                                    ))

                                prev_point = curr_point

                else:  # Edge path
                    # Extract from edge path
                    for edge in path.edges:
                        if edge.edge_type == 'LineEdge':
                            start = self._apply_transform((edge.start[0], edge.start[1]), transform)
                            end = self._apply_transform((edge.end[0], edge.end[1]), transform)

                            segments.append(Segment(
                                start=start,
                                end=end,
                                meta={'type': 'hatch_boundary', 'layer': hatch.dxf.layer}
                            ))

        except Exception as e:
            logger.warning(f"Failed to process HATCH entity: {e}")

        return segments

    def _bulge_to_arc_segments(self, start: Point, end: Point, bulge: float) -> List[Segment]:
        """
        Convert LWPOLYLINE bulge (arc segment) to line segments.

        Args:
            start: Start point
            end: End point
            bulge: Bulge value (tan(angle/4))

        Returns:
            List of line segments approximating the arc
        """
        # Calculate arc parameters
        dx = end.x - start.x
        dy = end.y - start.y
        chord_length = math.sqrt(dx * dx + dy * dy)

        # Arc angle from bulge
        angle = 4 * math.atan(abs(bulge))

        # Radius
        if angle < 1e-10:
            return [Segment(start=start, end=end, meta={'type': 'line'})]

        radius = chord_length / (2 * math.sin(angle / 2))

        # Center point
        mid_x = (start.x + end.x) / 2
        mid_y = (start.y + end.y) / 2

        # Perpendicular direction
        perp_x = -dy / chord_length
        perp_y = dx / chord_length

        # Distance from chord midpoint to arc center
        sagitta = radius - math.sqrt(radius * radius - (chord_length / 2) ** 2)

        # Adjust for bulge sign (direction)
        if bulge < 0:
            sagitta = -sagitta

        center_x = mid_x + perp_x * sagitta * math.cos(angle / 2)
        center_y = mid_y + perp_y * sagitta * math.cos(angle / 2)

        center = Point(x=center_x, y=center_y)

        # Start and end angles
        start_angle = math.atan2(start.y - center.y, start.x - center.x)
        end_angle = math.atan2(end.y - center.y, end.x - center.x)

        # Adjust for bulge sign
        if bulge < 0:
            if end_angle > start_angle:
                end_angle -= 2 * math.pi
        else:
            if end_angle < start_angle:
                end_angle += 2 * math.pi

        # Generate arc segments
        num_segments = 20
        angle_step = (end_angle - start_angle) / num_segments

        segments = []
        prev_point = start

        for i in range(1, num_segments + 1):
            angle = start_angle + i * angle_step
            x = center.x + radius * math.cos(angle)
            y = center.y + radius * math.sin(angle)
            curr_point = Point(x=x, y=y)

            segments.append(Segment(
                start=prev_point,
                end=curr_point,
                meta={
                    'type': 'arc_segment',
                    'center': (center.x, center.y),
                    'radius': radius
                }
            ))

            prev_point = curr_point

        return segments

    def _calculate_bbox(self, segments: List[Segment]) -> Dict[str, float]:
        """
        Calculate bounding box from all segment endpoints.

        Args:
            segments: List of segments

        Returns:
            Dict with minX, minY, maxX, maxY
        """
        if not segments:
            return {'minX': 0, 'minY': 0, 'maxX': 0, 'maxY': 0}

        min_x = float('inf')
        min_y = float('inf')
        max_x = float('-inf')
        max_y = float('-inf')

        for seg in segments:
            min_x = min(min_x, seg.start.x, seg.end.x)
            min_y = min(min_y, seg.start.y, seg.end.y)
            max_x = max(max_x, seg.start.x, seg.end.x)
            max_y = max(max_y, seg.start.y, seg.end.y)

        return {
            'minX': min_x,
            'minY': min_y,
            'maxX': max_x,
            'maxY': max_y
        }
