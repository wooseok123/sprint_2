"""
DXF Parser and segment normalizer.

This module loads DXF files, expands INSERT trees, and converts raw geometric
entities into normalized segments. Heuristic cleanup lives in a dedicated
preprocessing stage.
"""
from collections import deque
from dataclasses import dataclass, field
import math
import logging
import re
from typing import List, Dict, Tuple, Any, Optional, Set

import ezdxf
import numpy as np
from ezdxf.entities import DXFEntity
from ezdxf.math import Matrix44, Vec3
from shapely.geometry import GeometryCollection, LineString, MultiLineString, Point as ShapelyPoint, Polygon

try:
    from scipy.spatial import cKDTree
except ImportError:  # pragma: no cover - optional runtime acceleration
    cKDTree = None

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
    flattened_entities: List["FlattenedEntity"] = field(default_factory=list)
    preprocessing: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FlattenedEntity:
    """A renderable DXF entity resolved into world-space context."""
    entity: DXFEntity
    transform: Optional[Matrix44]
    effective_layer: Optional[str]
    effective_linetype: Optional[str]
    block_path: Tuple[str, ...] = ()
    clip_boundaries: Tuple[Polygon, ...] = ()


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
    GEOMETRY_ENTITY_TYPES = {'LINE', 'LWPOLYLINE', 'POLYLINE', 'SPLINE', 'ARC'}
    BLOCK_REFERENCE_TYPES = {'INSERT'}
    FORCED_DELETE_TYPES = {'TEXT', 'MTEXT', 'DIMENSION', 'HATCH', 'LEADER', 'TOLERANCE', 'POINT', 'SOLID', '3DFACE'}
    TITLE_SIGNAL_TYPES = {'TEXT', 'MTEXT', 'DIMENSION', 'LEADER', 'TOLERANCE'}
    MIN_WALL_THICKNESS_MM = 5.0
    MAX_WALL_THICKNESS_MM = 300.0
    BREAKLINE_NAME_KEYWORDS = ('break', 'brk', '파단', '절단')
    ANNOTATION_LAYER_KEYWORDS = ('sym', 'anno')
    ANNOTATION_NAME_TOKENS = (
        'anno', 'annotation', 'sym', 'symbol', 'dim', 'dims', 'dimension',
        'center', 'centre', 'centerline', 'cen', 'ctr', 'cntr', 'text', 'note',
    )
    ANNOTATION_LINETYPE_PREFIXES = ('center', 'centre', 'dim')
    FILTERED_LINETYPE_PREFIXES = ('center', 'centre', 'dashdot', 'phantom', 'dashed')
    FILTERED_LINETYPE_NAMES = {
        'CENTER', 'CENTER2', 'CENTERX2',
        'DASHDOT', 'DASHDOT2',
        'PHANTOM', 'PHANTOM2',
        'DASHED', 'DASHED2',
    }
    DIMENSION_BLOCK_PREFIXES = ('*d',)
    TITLE_BLOCK_GRID_SIZE = 10
    FRAME_EDGE_TOLERANCE_RATIO = 0.02
    FRAME_MIN_INNER_ENTITY_COUNT = 1
    FRAME_MIN_INSET_RATIO = 0.03

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
        self._arc_group_counter = 0

    def parse(
        self,
        *,
        build_segments: bool = True,
        build_hatches: bool = True,
    ) -> ParsedDXF:
        """
        Parse DXF file and normalize raw geometry to segments.

        Returns:
            ParsedDXF with raw segments, HATCH entities, bbox, units, and flattening context
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

        # Collect all raw geometry
        self._arc_group_counter = 0
        segments: List[Segment] = []
        hatch_entities: List[Segment] = []

        # Process modelspace entities
        msp = self.doc.modelspace()
        entity_count = 0
        flattened_entities: List[FlattenedEntity] = []

        for entity in msp:
            if not self._should_collect_entity(entity):
                continue

            entity_count += 1

            try:
                flattened_entities.extend(
                    self._flatten_entity(
                        entity,
                        visited=set(),
                        parent_transform=None,
                        parent_layer=None,
                        parent_linetype=None,
                        block_path=(),
                        clip_boundaries=(),
                    )
                )
            except Exception as e:
                logger.warning(f"Failed to process entity {entity.dxftype()}: {e}")
                continue

        if build_segments or build_hatches:
            for flattened in flattened_entities:
                dxftype = flattened.entity.dxftype()
                try:
                    if build_segments and dxftype in self.GEOMETRY_ENTITY_TYPES:
                        segments.extend(self._process_flattened_entity(flattened))
                    elif build_hatches and dxftype == 'HATCH':
                        hatch_entities.extend(self._process_hatch(flattened.entity, flattened.transform))
                except Exception as e:
                    logger.warning(f"Failed to normalize flattened entity {dxftype}: {e}")
                    continue

        logger.info(
            "Processed %d top-level entities, flattened %d entities, extracted %d raw segments",
            entity_count,
            len(flattened_entities),
            len(segments),
        )
        logger.info(f"Collected {len(hatch_entities)} HATCH entities for validation")

        # Calculate bounding box
        bbox_dict = self._calculate_bbox(segments) if segments else self._calculate_flattened_bbox(flattened_entities)
        logger.info(f"Bounding box: {bbox_dict}")

        return ParsedDXF(
            segments=segments,
            hatch_entities=hatch_entities,
            bbox=bbox_dict,
            units=units,
            insunits_code=self.insunits_code,
            unit_scale_to_mm=self.unit_scale_to_mm,
            entity_count=entity_count,
            flattened_entities=flattened_entities,
            preprocessing={},
        )

    def _flatten_entity(
        self,
        entity: DXFEntity,
        visited: Set[str],
        parent_transform: Optional[Matrix44],
        parent_layer: Optional[str],
        parent_linetype: Optional[str],
        block_path: Tuple[str, ...],
        clip_boundaries: Tuple[Polygon, ...],
    ) -> List[FlattenedEntity]:
        """Explode INSERT trees into leaf entities while preserving inheritance context."""
        if not self._is_entity_renderable(entity):
            return []

        dxftype = entity.dxftype()
        effective_layer = self._resolve_effective_layer(entity, parent_layer)
        effective_linetype = self._resolve_effective_linetype(entity, effective_layer, parent_linetype)

        if dxftype in self.BLOCK_REFERENCE_TYPES:
            block_name = getattr(entity.dxf, 'name', None)
            if not block_name:
                return []
            if block_name in visited:
                logger.warning(f"Circular block reference detected: {block_name}")
                return []

            insert_transform = self._build_insert_transform(entity)
            combined_transform = self._combine_transforms(parent_transform, insert_transform)

            try:
                block_def = self.doc.blocks.get(block_name)
            except ValueError:
                logger.warning(f"Block definition not found: {block_name}")
                return []

            next_clip_boundaries = clip_boundaries
            insert_clip = self._extract_insert_clip_boundary(entity, combined_transform)
            if insert_clip is not None:
                next_clip_boundaries = clip_boundaries + (insert_clip,)

            new_visited = visited.copy()
            new_visited.add(block_name)
            flattened: List[FlattenedEntity] = []
            next_block_path = block_path + (str(block_name),)
            for child in block_def:
                flattened.extend(
                    self._flatten_entity(
                        child,
                        visited=new_visited,
                        parent_transform=combined_transform,
                        parent_layer=effective_layer,
                        parent_linetype=effective_linetype,
                        block_path=next_block_path,
                        clip_boundaries=next_clip_boundaries,
                    )
                )
            return flattened

        if clip_boundaries and not self._entity_intersects_clip_boundaries(entity, parent_transform, clip_boundaries):
            return []

        return [
            FlattenedEntity(
                entity=entity,
                transform=parent_transform,
                effective_layer=effective_layer,
                effective_linetype=effective_linetype,
                block_path=block_path,
                clip_boundaries=clip_boundaries,
            )
        ]

    def _preprocess_entities(
        self,
        flattened_entities: List[FlattenedEntity],
    ) -> Tuple[List[Segment], List[Segment], Dict[str, Any]]:
        """Apply DXF cleanup heuristics before graph construction."""
        if not flattened_entities:
            return [], [], {
                "flattened_entities": 0,
                "geometry_entities_after_filters": 0,
                "removed_by_type": 0,
                "removed_by_annotation": 0,
                "removed_by_linetype": 0,
                "removed_by_title_block": 0,
                "removed_by_border_frame": 0,
                "removed_short_segments": 0,
                "removed_isolated_segments": 0,
            }

        hatch_entities = self._collect_hatch_segments(flattened_entities)
        removed_ids: Set[int] = set()
        removed_by_type: Set[int] = set()
        removed_by_annotation: Set[int] = set()
        removed_by_linetype: Set[int] = set()

        geometry_candidates: List[Tuple[int, FlattenedEntity]] = []

        for index, flattened in enumerate(flattened_entities):
            dxftype = flattened.entity.dxftype()

            if dxftype in self.FORCED_DELETE_TYPES:
                removed_ids.add(index)
                removed_by_type.add(index)
                continue

            if self._is_breakline_flattened_entity(flattened) or self._is_annotation_flattened_entity(flattened):
                removed_ids.add(index)
                removed_by_annotation.add(index)
                continue

            if self._matches_filtered_linetype(flattened.effective_linetype):
                removed_ids.add(index)
                removed_by_linetype.add(index)
                continue

            if dxftype in self.GEOMETRY_ENTITY_TYPES:
                geometry_candidates.append((index, flattened))

        drawing_bbox = self._calculate_flattened_bbox(flattened_entities)

        title_block_bbox = self._detect_title_block_bbox(
            flattened_entities,
            drawing_bbox,
            excluded_ids=removed_by_annotation | removed_by_linetype,
        )
        removed_by_title_block: Set[int] = set()
        if title_block_bbox is not None:
            for index, flattened in enumerate(flattened_entities):
                if self._flattened_entity_intersects_bbox(flattened, title_block_bbox):
                    removed_ids.add(index)
                    removed_by_title_block.add(index)

        remaining_geometry = [
            (index, flattened)
            for index, flattened in geometry_candidates
            if index not in removed_ids
        ]

        border_frame_index = self._detect_border_frame_index(remaining_geometry, drawing_bbox)
        removed_by_border_frame: Set[int] = set()
        border_frame_bbox = None
        if border_frame_index is not None:
            removed_ids.add(border_frame_index)
            removed_by_border_frame.add(border_frame_index)
            border_frame_bbox = self._entity_bbox_dict(flattened_entities[border_frame_index])

        segments = []
        for index, flattened in geometry_candidates:
            if index in removed_ids:
                continue
            segments.extend(self._process_flattened_entity(flattened))

        segments, short_segment_meta = self._remove_short_segments(segments, drawing_bbox)
        segments, isolation_meta = self._remove_isolated_segments(segments, drawing_bbox)

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
            "removed_short_segments": short_segment_meta["removed_segments"],
            "removed_isolated_segments": isolation_meta["removed_segments"],
            "removed_isolated_components": isolation_meta["removed_components"],
            "short_segment_threshold_mm": short_segment_meta["threshold"],
            "title_block_bbox": title_block_bbox,
            "border_frame_bbox": border_frame_bbox,
            "drawing_bbox_before_cleanup": drawing_bbox,
            "segments_after_preprocessing": len(segments),
        }
        return segments, hatch_entities, preprocessing

    def _collect_hatch_segments(self, flattened_entities: List[FlattenedEntity]) -> List[Segment]:
        """Keep HATCH boundaries only for late validation, not for outline extraction."""
        hatch_segments: List[Segment] = []
        for flattened in flattened_entities:
            if flattened.entity.dxftype() != 'HATCH':
                continue
            hatch_segments.extend(self._process_hatch(flattened.entity, flattened.transform))
        return hatch_segments

    def _process_flattened_entity(self, flattened: FlattenedEntity) -> List[Segment]:
        """Convert a flattened leaf entity into normalized segments."""
        segments = self._process_entity(flattened.entity, flattened.transform)
        if flattened.clip_boundaries:
            segments = self._clip_segments_to_boundaries(segments, flattened.clip_boundaries)
        for segment in segments:
            segment.meta["layer"] = flattened.effective_layer or segment.meta.get("layer")
            if flattened.effective_linetype:
                segment.meta["effective_linetype"] = flattened.effective_linetype
            if flattened.block_path:
                segment.meta["block_path"] = list(flattened.block_path)
        return segments

    def _extract_insert_clip_boundary(
        self,
        insert_entity: DXFEntity,
        world_transform: Optional[Matrix44],
    ) -> Optional[Polygon]:
        """Resolve an INSERT XCLIP/SPATIAL_FILTER into a world-space polygon."""
        if not getattr(insert_entity, "has_extension_dict", False):
            return None

        try:
            extension_dict = insert_entity.get_extension_dict()
        except Exception:
            return None

        if "ACAD_FILTER" not in extension_dict:
            return None

        try:
            filter_dict = extension_dict["ACAD_FILTER"]
        except Exception:
            return None

        spatial_filter = None
        try:
            if hasattr(filter_dict, "keys"):
                for key in filter_dict.keys():
                    candidate = filter_dict[key]
                    if candidate.dxftype() == "SPATIAL_FILTER":
                        spatial_filter = candidate
                        break
        except Exception:
            return None

        if spatial_filter is None or not bool(getattr(spatial_filter.dxf, "is_clipping_enabled", 0)):
            return None

        try:
            local_vertices = [
                spatial_filter.transform_matrix.transform(
                    spatial_filter.inverse_insert_matrix.transform((vertex.x, vertex.y, 0.0))
                )
                for vertex in spatial_filter.boundary_vertices
            ]
        except Exception as exc:
            logger.debug("Failed to resolve SPATIAL_FILTER for INSERT %s: %s", insert_entity.dxftype(), exc)
            return None

        if len(local_vertices) < 3:
            return None

        world_vertices = [
            self._apply_transform((vertex.x, vertex.y), world_transform).to_2d()
            for vertex in local_vertices
        ]
        polygon = Polygon(world_vertices)
        if polygon.is_empty:
            return None
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty or polygon.area <= 0:
            return None
        return polygon

    def _entity_intersects_clip_boundaries(
        self,
        entity: DXFEntity,
        transform: Optional[Matrix44],
        clip_boundaries: Tuple[Polygon, ...],
    ) -> bool:
        geometry = self._entity_clip_geometry(entity, transform)
        if geometry is None:
            return True

        clipped = geometry
        for clip_boundary in clip_boundaries:
            try:
                clipped = clipped.intersection(clip_boundary)
            except Exception:
                return True
            if clipped.is_empty:
                return False
        return not clipped.is_empty

    def _entity_clip_geometry(
        self,
        entity: DXFEntity,
        transform: Optional[Matrix44],
    ):
        dxftype = entity.dxftype()
        try:
            if dxftype == "LINE":
                start = self._apply_transform((entity.dxf.start.x, entity.dxf.start.y), transform).to_2d()
                end = self._apply_transform((entity.dxf.end.x, entity.dxf.end.y), transform).to_2d()
                return LineString([start, end])
            if dxftype in {"LWPOLYLINE", "POLYLINE"}:
                points = [point.to_2d() for point in self._get_polyline_points(entity, transform=transform)]
                if len(points) < 2:
                    return ShapelyPoint(points[0]) if points else None
                if self._polyline_is_closed(entity) and points[0] != points[-1]:
                    points.append(points[0])
                return LineString(points)
            if dxftype == "SPLINE":
                points = [
                    self._apply_transform((point[0], point[1]), transform).to_2d()
                    for point in entity.discretize(segment_count=24).vertices_in_wcs()
                ]
                if len(points) < 2:
                    return ShapelyPoint(points[0]) if points else None
                return LineString(points)
            if dxftype == "ARC":
                points = []
                start_angle = float(entity.dxf.start_angle)
                end_angle = float(entity.dxf.end_angle)
                if end_angle <= start_angle:
                    end_angle += 360.0
                steps = max(8, int((end_angle - start_angle) / 15.0))
                for index in range(steps + 1):
                    angle = start_angle + (end_angle - start_angle) * index / steps
                    points.append(self._apply_transform(self._arc_point(entity, angle), transform).to_2d())
                return LineString(points)
            if dxftype in {"TEXT", "MTEXT", "POINT"}:
                insert = getattr(entity.dxf, "insert", None) or getattr(entity.dxf, "location", None)
                if insert is None:
                    return None
                point = self._apply_transform((insert.x, insert.y), transform).to_2d()
                return ShapelyPoint(point)
        except Exception:
            logger.debug("Failed to build clip geometry for %s", dxftype)

        bbox = self._entity_bbox_dict(
            FlattenedEntity(
                entity=entity,
                transform=transform,
                effective_layer=None,
                effective_linetype=None,
            )
        )
        if bbox is None:
            return None
        return Polygon([
            (bbox["minX"], bbox["minY"]),
            (bbox["maxX"], bbox["minY"]),
            (bbox["maxX"], bbox["maxY"]),
            (bbox["minX"], bbox["maxY"]),
        ])

    def _clip_segments_to_boundaries(
        self,
        segments: List[Segment],
        clip_boundaries: Tuple[Polygon, ...],
    ) -> List[Segment]:
        clipped_segments: List[Segment] = []
        for segment in segments:
            geometry = LineString([segment.start.to_2d(), segment.end.to_2d()])
            for clip_boundary in clip_boundaries:
                geometry = geometry.intersection(clip_boundary)
                if geometry.is_empty:
                    break

            if geometry.is_empty:
                continue

            clipped_segments.extend(self._segment_parts_from_geometry(geometry, segment.meta))
        return clipped_segments

    def _segment_parts_from_geometry(
        self,
        geometry,
        meta: Dict[str, Any],
    ) -> List[Segment]:
        if geometry.is_empty:
            return []
        if isinstance(geometry, LineString):
            return self._segments_from_linestring(geometry, meta)
        if isinstance(geometry, MultiLineString):
            parts: List[Segment] = []
            for line in geometry.geoms:
                parts.extend(self._segments_from_linestring(line, meta))
            return parts
        if isinstance(geometry, GeometryCollection):
            parts: List[Segment] = []
            for item in geometry.geoms:
                parts.extend(self._segment_parts_from_geometry(item, meta))
            return parts
        return []

    def _segments_from_linestring(
        self,
        linestring: LineString,
        meta: Dict[str, Any],
    ) -> List[Segment]:
        coords = list(linestring.coords)
        parts: List[Segment] = []
        for start, end in zip(coords, coords[1:]):
            if start == end:
                continue
            parts.append(
                Segment(
                    start=Point(float(start[0]), float(start[1])),
                    end=Point(float(end[0]), float(end[1])),
                    meta=dict(meta),
                )
            )
        return parts

    def _resolve_effective_layer(
        self,
        entity: DXFEntity,
        parent_layer: Optional[str],
    ) -> Optional[str]:
        """Resolve layer 0 inheritance for block content."""
        layer_name = getattr(entity.dxf, 'layer', None)
        if layer_name in (None, '', '0'):
            return parent_layer or layer_name
        return str(layer_name)

    def _resolve_effective_linetype(
        self,
        entity: DXFEntity,
        effective_layer: Optional[str],
        parent_linetype: Optional[str],
    ) -> Optional[str]:
        """Resolve BYLAYER/BYBLOCK linetype inheritance into a concrete value when possible."""
        raw_linetype = getattr(entity.dxf, 'linetype', None)
        normalized = self._normalize_linetype_name(raw_linetype)

        if normalized in (None, '', 'BYLAYER'):
            layer_linetype = self._get_layer_linetype(effective_layer)
            if layer_linetype not in (None, '', 'BYLAYER', 'BYBLOCK'):
                return layer_linetype
            if layer_linetype == 'BYBLOCK' and parent_linetype not in (None, '', 'BYLAYER', 'BYBLOCK'):
                return parent_linetype
            return layer_linetype or 'BYLAYER'

        if normalized == 'BYBLOCK':
            if parent_linetype not in (None, '', 'BYLAYER', 'BYBLOCK'):
                return parent_linetype
            layer_linetype = self._get_layer_linetype(effective_layer)
            if layer_linetype not in (None, '', 'BYLAYER', 'BYBLOCK'):
                return layer_linetype
            return layer_linetype or 'BYBLOCK'

        return normalized

    def _get_layer_linetype(self, layer_name: Optional[str]) -> Optional[str]:
        if self.doc is None or not layer_name:
            return None
        try:
            layer = self.doc.layers.get(layer_name)
        except Exception:
            return None
        return self._normalize_linetype_name(getattr(layer.dxf, 'linetype', None))

    def _normalize_linetype_name(self, name: Optional[str]) -> Optional[str]:
        if name is None:
            return None
        normalized = str(name).strip()
        if not normalized:
            return None
        return normalized.upper()

    def _is_breakline_flattened_entity(self, flattened: FlattenedEntity) -> bool:
        layer_name = flattened.effective_layer
        if self._matches_breakline_name(layer_name):
            return True
        if any(self._matches_breakline_name(name) for name in flattened.block_path):
            return True
        if self._matches_annotation_layer_name(layer_name) and self._looks_like_breakline_polyline(
            flattened.entity,
            transform=flattened.transform,
        ):
            return True
        return False

    def _is_annotation_flattened_entity(self, flattened: FlattenedEntity) -> bool:
        """Match open annotation geometry after block/layer inheritance has been resolved."""
        entity = flattened.entity
        layer_name = flattened.effective_layer
        linetype_name = flattened.effective_linetype

        if not self._is_open_annotation_geometry(entity):
            return False

        return (
            self._matches_annotation_name(layer_name)
            or any(self._matches_annotation_name(name) for name in flattened.block_path)
            or self._matches_annotation_linetype_name(linetype_name)
        )

    def _matches_filtered_linetype(self, name: Optional[str]) -> bool:
        """Delete centerlines and dashed reference lines regardless of layer naming."""
        if not name:
            return False

        normalized = self._normalize_linetype_name(name)
        if normalized in self.FILTERED_LINETYPE_NAMES:
            return True

        compact = normalized.replace('-', '').replace('_', '')
        for prefix in self.FILTERED_LINETYPE_PREFIXES:
            normalized_prefix = prefix.upper().replace('-', '').replace('_', '')
            if compact.startswith(normalized_prefix):
                return True
        return False

    def _calculate_flattened_bbox(self, flattened_entities: List[FlattenedEntity]) -> Dict[str, float]:
        points: List[Point] = []
        for flattened in flattened_entities:
            points.extend(self._flattened_entity_points(flattened))
        if not points:
            return {'minX': 0, 'minY': 0, 'maxX': 0, 'maxY': 0}

        return {
            'minX': min(point.x for point in points),
            'minY': min(point.y for point in points),
            'maxX': max(point.x for point in points),
            'maxY': max(point.y for point in points),
        }

    def _flattened_entity_points(self, flattened: FlattenedEntity) -> List[Point]:
        entity = flattened.entity
        dxftype = entity.dxftype()
        transform = flattened.transform

        try:
            if dxftype == 'LINE':
                return [
                    self._apply_transform((entity.dxf.start.x, entity.dxf.start.y), transform),
                    self._apply_transform((entity.dxf.end.x, entity.dxf.end.y), transform),
                ]
            if dxftype in {'LWPOLYLINE', 'POLYLINE'}:
                return self._get_polyline_points(entity, transform=transform)
            if dxftype == 'SPLINE':
                discretized = entity.discretize(segment_count=24)
                return [
                    self._apply_transform((point[0], point[1]), transform)
                    for point in discretized.vertices_in_wcs()
                ]
            if dxftype == 'ARC':
                center = self._apply_transform((entity.dxf.center.x, entity.dxf.center.y), transform)
                start = self._apply_transform(self._arc_point(entity, entity.dxf.start_angle), transform)
                end = self._apply_transform(self._arc_point(entity, entity.dxf.end_angle), transform)
                return [center, start, end]
            if dxftype in {'TEXT', 'MTEXT', 'POINT'}:
                insert = getattr(entity.dxf, 'insert', None) or getattr(entity.dxf, 'location', None)
                if insert is not None:
                    return [self._apply_transform((insert.x, insert.y), transform)]
        except Exception:
            logger.debug("Failed to approximate points for entity type %s", dxftype)

        return []

    def _arc_point(self, arc: DXFEntity, angle_deg: float) -> Tuple[float, float]:
        angle = math.radians(angle_deg)
        return (
            arc.dxf.center.x + arc.dxf.radius * math.cos(angle),
            arc.dxf.center.y + arc.dxf.radius * math.sin(angle),
        )

    def _entity_bbox_dict(self, flattened: FlattenedEntity) -> Optional[Dict[str, float]]:
        points = self._flattened_entity_points(flattened)
        if not points:
            return None
        return {
            'minX': min(point.x for point in points),
            'minY': min(point.y for point in points),
            'maxX': max(point.x for point in points),
            'maxY': max(point.y for point in points),
        }

    def _flattened_entity_center(self, flattened: FlattenedEntity) -> Optional[Tuple[float, float]]:
        bbox = self._entity_bbox_dict(flattened)
        if bbox is None:
            return None
        return (
            (bbox['minX'] + bbox['maxX']) / 2.0,
            (bbox['minY'] + bbox['maxY']) / 2.0,
        )

    def _flattened_entity_intersects_bbox(
        self,
        flattened: FlattenedEntity,
        bbox: Dict[str, float],
    ) -> bool:
        entity_bbox = self._entity_bbox_dict(flattened)
        if entity_bbox is None:
            center = self._flattened_entity_center(flattened)
            if center is None:
                return False
            return bbox['minX'] <= center[0] <= bbox['maxX'] and bbox['minY'] <= center[1] <= bbox['maxY']

        return not (
            entity_bbox['maxX'] < bbox['minX']
            or entity_bbox['minX'] > bbox['maxX']
            or entity_bbox['maxY'] < bbox['minY']
            or entity_bbox['minY'] > bbox['maxY']
        )

    def _detect_title_block_bbox(
        self,
        flattened_entities: List[FlattenedEntity],
        drawing_bbox: Dict[str, float],
        excluded_ids: Set[int],
    ) -> Optional[Dict[str, float]]:
        """Detect dense title blocks attached to the sheet edge."""
        width = drawing_bbox['maxX'] - drawing_bbox['minX']
        height = drawing_bbox['maxY'] - drawing_bbox['minY']
        if width <= 0 or height <= 0:
            return None

        grid_size = self.TITLE_BLOCK_GRID_SIZE
        cell_width = width / grid_size
        cell_height = height / grid_size
        if cell_width <= 0 or cell_height <= 0:
            return None

        line_lengths = []
        for index, flattened in enumerate(flattened_entities):
            if index in excluded_ids:
                continue
            if flattened.entity.dxftype() != 'LINE':
                continue
            try:
                start = self._apply_transform((flattened.entity.dxf.start.x, flattened.entity.dxf.start.y), flattened.transform)
                end = self._apply_transform((flattened.entity.dxf.end.x, flattened.entity.dxf.end.y), flattened.transform)
                line_lengths.append(math.hypot(end.x - start.x, end.y - start.y))
            except Exception:
                continue

        diagonal = math.hypot(width, height)
        short_line_threshold = min(
            float(np.percentile(line_lengths, 35)) if line_lengths else diagonal * 0.03,
            diagonal * 0.03,
        )

        density = [[0.0 for _ in range(grid_size)] for _ in range(grid_size)]

        for index, flattened in enumerate(flattened_entities):
            if index in excluded_ids:
                continue

            dxftype = flattened.entity.dxftype()
            center = self._flattened_entity_center(flattened)
            if center is None:
                continue

            col = min(grid_size - 1, max(0, int((center[0] - drawing_bbox['minX']) / cell_width)))
            row = min(grid_size - 1, max(0, int((center[1] - drawing_bbox['minY']) / cell_height)))

            if dxftype in self.TITLE_SIGNAL_TYPES:
                density[row][col] += 2.0
                continue

            if dxftype == 'LINE':
                bbox = self._entity_bbox_dict(flattened)
                if bbox is None:
                    continue
                length = math.hypot(bbox['maxX'] - bbox['minX'], bbox['maxY'] - bbox['minY'])
                if length <= short_line_threshold:
                    density[row][col] += 1.0

        non_zero_scores = [score for row in density for score in row if score > 0]
        if not non_zero_scores:
            return self._detect_title_block_by_rectangle(flattened_entities, drawing_bbox, excluded_ids)

        threshold = max(4.0, float(np.percentile(non_zero_scores, 80)))
        visited = set()
        best_cluster = None
        best_score = 0.0

        for row in range(grid_size):
            for col in range(grid_size):
                if (row, col) in visited or density[row][col] < threshold:
                    continue
                cluster, cluster_score = self._collect_density_cluster(density, row, col, threshold, visited)
                if not cluster:
                    continue
                if not any(self._cell_is_edge(r, c, grid_size) for r, c in cluster):
                    continue
                if cluster_score > best_score:
                    best_cluster = cluster
                    best_score = cluster_score

        if best_cluster is not None:
            rows = [row for row, _ in best_cluster]
            cols = [col for _, col in best_cluster]
            bbox = {
                'minX': drawing_bbox['minX'] + min(cols) * cell_width,
                'minY': drawing_bbox['minY'] + min(rows) * cell_height,
                'maxX': drawing_bbox['minX'] + (max(cols) + 1) * cell_width,
                'maxY': drawing_bbox['minY'] + (max(rows) + 1) * cell_height,
            }
            if best_score >= 6.0:
                return bbox

        return self._detect_title_block_by_rectangle(flattened_entities, drawing_bbox, excluded_ids)

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

    def _detect_title_block_by_rectangle(
        self,
        flattened_entities: List[FlattenedEntity],
        drawing_bbox: Dict[str, float],
        excluded_ids: Set[int],
    ) -> Optional[Dict[str, float]]:
        """Fallback title block detection via large inner rectangles packed with signals."""
        rectangle_candidates: List[Tuple[float, int, Dict[str, float]]] = []
        drawing_area = (drawing_bbox['maxX'] - drawing_bbox['minX']) * (drawing_bbox['maxY'] - drawing_bbox['minY'])
        if drawing_area <= 0:
            return None

        for index, flattened in enumerate(flattened_entities):
            if index in excluded_ids:
                continue
            bbox = self._entity_bbox_dict(flattened)
            if bbox is None:
                continue
            rect_area = (bbox['maxX'] - bbox['minX']) * (bbox['maxY'] - bbox['minY'])
            if rect_area <= 0 or rect_area >= drawing_area * 0.8:
                continue
            if not self._is_closed_rectangle(flattened):
                continue
            rectangle_candidates.append((rect_area, index, bbox))

        rectangle_candidates.sort(reverse=True)
        if len(rectangle_candidates) < 2:
            return None

        for _, _, bbox in rectangle_candidates[1:3]:
            signal_count = 0
            short_line_count = 0
            diagonal = math.hypot(drawing_bbox['maxX'] - drawing_bbox['minX'], drawing_bbox['maxY'] - drawing_bbox['minY'])
            for index, flattened in enumerate(flattened_entities):
                if index in excluded_ids:
                    continue
                center = self._flattened_entity_center(flattened)
                if center is None:
                    continue
                if not (bbox['minX'] <= center[0] <= bbox['maxX'] and bbox['minY'] <= center[1] <= bbox['maxY']):
                    continue
                if flattened.entity.dxftype() in self.TITLE_SIGNAL_TYPES:
                    signal_count += 1
                elif flattened.entity.dxftype() == 'LINE':
                    entity_bbox = self._entity_bbox_dict(flattened)
                    if entity_bbox is None:
                        continue
                    length = math.hypot(entity_bbox['maxX'] - entity_bbox['minX'], entity_bbox['maxY'] - entity_bbox['minY'])
                    if length <= diagonal * 0.03:
                        short_line_count += 1

            if signal_count >= 2 and (short_line_count >= 2 or signal_count >= 3):
                return bbox

        return None

    def _detect_border_frame_index(
        self,
        geometry_entities: List[Tuple[int, FlattenedEntity]],
        drawing_bbox: Dict[str, float],
    ) -> Optional[int]:
        """Remove the dominant outer sheet frame when it matches a simple rectangle."""
        if len(geometry_entities) < 2:
            return None

        drawing_area = (drawing_bbox['maxX'] - drawing_bbox['minX']) * (drawing_bbox['maxY'] - drawing_bbox['minY'])
        if drawing_area <= 0:
            return None

        best_index = None
        best_area = 0.0
        for index, flattened in geometry_entities:
            if not self._is_closed_rectangle(flattened):
                continue
            bbox = self._entity_bbox_dict(flattened)
            if bbox is None:
                continue
            area = (bbox['maxX'] - bbox['minX']) * (bbox['maxY'] - bbox['minY'])
            if area < drawing_area * 0.85:
                continue
            if not self._bbox_is_edge_anchored(bbox, drawing_bbox):
                continue
            if not self._looks_like_sheet_frame(index, bbox, geometry_entities):
                continue
            if area > best_area:
                best_area = area
                best_index = index
        return best_index

    def _looks_like_sheet_frame(
        self,
        candidate_index: int,
        candidate_bbox: Dict[str, float],
        geometry_entities: List[Tuple[int, FlattenedEntity]],
    ) -> bool:
        inner_bboxes: List[Dict[str, float]] = []
        for index, flattened in geometry_entities:
            if index == candidate_index:
                continue
            bbox = self._entity_bbox_dict(flattened)
            if bbox is None:
                continue
            if not self._bbox_contains(candidate_bbox, bbox, margin=1e-6):
                continue
            inner_bboxes.append(bbox)

        if len(inner_bboxes) < self.FRAME_MIN_INNER_ENTITY_COUNT:
            return False

        inner_bbox = {
            'minX': min(bbox['minX'] for bbox in inner_bboxes),
            'minY': min(bbox['minY'] for bbox in inner_bboxes),
            'maxX': max(bbox['maxX'] for bbox in inner_bboxes),
            'maxY': max(bbox['maxY'] for bbox in inner_bboxes),
        }

        width = candidate_bbox['maxX'] - candidate_bbox['minX']
        height = candidate_bbox['maxY'] - candidate_bbox['minY']
        if width <= 0 or height <= 0:
            return False

        inset_x = width * self.FRAME_MIN_INSET_RATIO
        inset_y = height * self.FRAME_MIN_INSET_RATIO
        return (
            inner_bbox['minX'] - candidate_bbox['minX'] >= inset_x
            and candidate_bbox['maxX'] - inner_bbox['maxX'] >= inset_x
            and inner_bbox['minY'] - candidate_bbox['minY'] >= inset_y
            and candidate_bbox['maxY'] - inner_bbox['maxY'] >= inset_y
        )

    def _bbox_is_edge_anchored(
        self,
        candidate_bbox: Dict[str, float],
        drawing_bbox: Dict[str, float],
    ) -> bool:
        draw_width = max(drawing_bbox['maxX'] - drawing_bbox['minX'], 0.0)
        draw_height = max(drawing_bbox['maxY'] - drawing_bbox['minY'], 0.0)
        if draw_width <= 0 or draw_height <= 0:
            return False

        tolerance_x = max(draw_width * self.FRAME_EDGE_TOLERANCE_RATIO, 1.0)
        tolerance_y = max(draw_height * self.FRAME_EDGE_TOLERANCE_RATIO, 1.0)
        return (
            abs(candidate_bbox['minX'] - drawing_bbox['minX']) <= tolerance_x
            and abs(candidate_bbox['maxX'] - drawing_bbox['maxX']) <= tolerance_x
            and abs(candidate_bbox['minY'] - drawing_bbox['minY']) <= tolerance_y
            and abs(candidate_bbox['maxY'] - drawing_bbox['maxY']) <= tolerance_y
        )

    def _bbox_contains(
        self,
        outer: Dict[str, float],
        inner: Dict[str, float],
        margin: float = 0.0,
    ) -> bool:
        return (
            inner['minX'] >= outer['minX'] - margin
            and inner['minY'] >= outer['minY'] - margin
            and inner['maxX'] <= outer['maxX'] + margin
            and inner['maxY'] <= outer['maxY'] + margin
        )

    def _is_closed_rectangle(self, flattened: FlattenedEntity) -> bool:
        entity = flattened.entity
        if entity.dxftype() not in {'LWPOLYLINE', 'POLYLINE'}:
            return False
        if not self._polyline_is_closed(entity):
            return False

        points = self._get_polyline_points(entity, transform=flattened.transform)
        if len(points) < 4:
            return False

        unique_points: List[Tuple[float, float]] = []
        for point in points:
            coord = (round(point.x, 6), round(point.y, 6))
            if coord not in unique_points:
                unique_points.append(coord)

        if len(unique_points) != 4:
            return False

        xs = sorted({coord[0] for coord in unique_points})
        ys = sorted({coord[1] for coord in unique_points})
        return len(xs) == 2 and len(ys) == 2

    def _remove_short_segments(
        self,
        segments: List[Segment],
        drawing_bbox: Dict[str, float],
    ) -> Tuple[List[Segment], Dict[str, Any]]:
        """Drop very short detail segments once enough geometry remains."""
        if len(segments) < 12:
            return segments, {"removed_segments": 0, "threshold": 0.0}

        lengths = [segment.length() for segment in segments if segment.length() > 1e-6]
        if not lengths:
            return segments, {"removed_segments": 0, "threshold": 0.0}

        diagonal = math.hypot(
            drawing_bbox['maxX'] - drawing_bbox['minX'],
            drawing_bbox['maxY'] - drawing_bbox['minY'],
        )
        percentile_threshold = float(np.percentile(lengths, 8))
        threshold = min(percentile_threshold, diagonal * 0.015)
        if threshold <= 0:
            return segments, {"removed_segments": 0, "threshold": 0.0}

        filtered = [segment for segment in segments if segment.length() > threshold]
        removed = len(segments) - len(filtered)
        if removed <= 0 or not filtered:
            return segments, {"removed_segments": 0, "threshold": threshold}
        return filtered, {"removed_segments": removed, "threshold": threshold}

    def _remove_isolated_segments(
        self,
        segments: List[Segment],
        drawing_bbox: Dict[str, float],
    ) -> Tuple[List[Segment], Dict[str, Any]]:
        """Trim components that are both far from the drawing centroid and very small."""
        if len(segments) < 6:
            return segments, {"removed_segments": 0, "removed_components": 0}

        endpoint_coords: List[Tuple[float, float]] = []
        endpoint_segment_ids: List[int] = []
        for index, segment in enumerate(segments):
            endpoint_coords.append(segment.start.to_2d())
            endpoint_segment_ids.append(index)
            endpoint_coords.append(segment.end.to_2d())
            endpoint_segment_ids.append(index)

        diagonal = math.hypot(
            drawing_bbox['maxX'] - drawing_bbox['minX'],
            drawing_bbox['maxY'] - drawing_bbox['minY'],
        )
        tolerance = max(diagonal * 0.015, 25.0)

        adjacency = {index: set() for index in range(len(segments))}
        if cKDTree is not None:
            tree = cKDTree(endpoint_coords)
            for endpoint_index, coord in enumerate(endpoint_coords):
                source_segment = endpoint_segment_ids[endpoint_index]
                for neighbor_index in tree.query_ball_point(coord, tolerance):
                    target_segment = endpoint_segment_ids[neighbor_index]
                    if source_segment == target_segment:
                        continue
                    adjacency[source_segment].add(target_segment)
                    adjacency[target_segment].add(source_segment)
        else:
            cell_size = max(tolerance, 1.0)
            buckets: Dict[Tuple[int, int], List[int]] = {}
            for endpoint_index, coord in enumerate(endpoint_coords):
                source_segment = endpoint_segment_ids[endpoint_index]
                cell_x = math.floor(coord[0] / cell_size)
                cell_y = math.floor(coord[1] / cell_size)

                for neighbor_cell_x in range(cell_x - 1, cell_x + 2):
                    for neighbor_cell_y in range(cell_y - 1, cell_y + 2):
                        for neighbor_index in buckets.get((neighbor_cell_x, neighbor_cell_y), []):
                            target_segment = endpoint_segment_ids[neighbor_index]
                            if source_segment == target_segment:
                                continue

                            neighbor_coord = endpoint_coords[neighbor_index]
                            if (
                                math.hypot(coord[0] - neighbor_coord[0], coord[1] - neighbor_coord[1])
                                > tolerance
                            ):
                                continue

                            adjacency[source_segment].add(target_segment)
                            adjacency[target_segment].add(source_segment)

                buckets.setdefault((cell_x, cell_y), []).append(endpoint_index)

        segment_midpoints = np.array([
            (
                (segment.start.x + segment.end.x) / 2.0,
                (segment.start.y + segment.end.y) / 2.0,
            )
            for segment in segments
        ])
        drawing_centroid = segment_midpoints.mean(axis=0)

        visited: Set[int] = set()
        remove_indices: Set[int] = set()
        removed_components = 0

        for index in range(len(segments)):
            if index in visited:
                continue
            queue = deque([index])
            visited.add(index)
            component = []
            while queue:
                current = queue.popleft()
                component.append(current)
                for neighbor in adjacency[current]:
                    if neighbor in visited:
                        continue
                    visited.add(neighbor)
                    queue.append(neighbor)

            if len(component) > max(3, int(len(segments) * 0.08)):
                continue

            component_midpoints = segment_midpoints[component]
            component_centroid = component_midpoints.mean(axis=0)
            distance = float(np.linalg.norm(component_centroid - drawing_centroid))
            if distance <= diagonal * 0.45:
                continue

            remove_indices.update(component)
            removed_components += 1

        if not remove_indices or len(remove_indices) == len(segments):
            return segments, {"removed_segments": 0, "removed_components": 0}

        filtered = [segment for index, segment in enumerate(segments) if index not in remove_indices]
        return filtered, {
            "removed_segments": len(remove_indices),
            "removed_components": removed_components,
        }

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

        if self._is_breakline_entity(entity, transform=transform):
            return [], []

        if self._is_annotation_entity(entity):
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

    def _should_collect_entity(self, entity: DXFEntity) -> bool:
        """Return True when the entity should be part of raw DXF extraction."""
        return self._is_entity_renderable(entity)

    def _is_breakline_entity(
        self,
        entity: DXFEntity,
        transform: Optional[Matrix44] = None,
    ) -> bool:
        """
        Exclude common breakline annotations before geometry extraction.

        In practice these are usually placed on dedicated BREAK/BREAKLINE style
        layers or inserted as named annotation blocks such as BREAKDATA.
        """
        layer_name = getattr(entity.dxf, 'layer', None)
        if self._matches_breakline_name(layer_name):
            return True

        if entity.dxftype() == 'INSERT':
            block_name = getattr(entity.dxf, 'name', None)
            if self._matches_breakline_name(block_name):
                return True

        if self._matches_annotation_layer_name(layer_name) and self._looks_like_breakline_polyline(
            entity,
            transform=transform,
        ):
            return True

        return False

    def _matches_breakline_name(self, name: Optional[str]) -> bool:
        """Match common breakline naming conventions using a loose substring check."""
        if not name:
            return False

        normalized = str(name).strip().lower()
        return any(keyword in normalized for keyword in self.BREAKLINE_NAME_KEYWORDS)

    def _matches_annotation_layer_name(self, name: Optional[str]) -> bool:
        """Match symbol/annotation layers where breaklines are often stored."""
        if not name:
            return False

        normalized = str(name).strip().lower()
        return any(keyword in normalized for keyword in self.ANNOTATION_LAYER_KEYWORDS)

    def _is_annotation_entity(self, entity: DXFEntity) -> bool:
        """
        Exclude common annotation-style geometry such as dimension and center lines.

        We keep this narrower than a blanket layer filter by only excluding open
        geometry, or whole INSERTs whose layer/block metadata clearly indicates
        annotation content.
        """
        layer_name = getattr(entity.dxf, 'layer', None)
        linetype_name = getattr(entity.dxf, 'linetype', None)

        if entity.dxftype() == 'INSERT':
            block_name = getattr(entity.dxf, 'name', None)
            return (
                self._matches_dimension_block_name(block_name)
                or self._matches_annotation_name(layer_name)
                or self._matches_annotation_name(block_name)
                or self._matches_annotation_linetype_name(linetype_name)
            )

        if not self._is_open_annotation_geometry(entity):
            return False

        return (
            self._matches_annotation_name(layer_name)
            or self._matches_annotation_linetype_name(linetype_name)
        )

    def _is_open_annotation_geometry(self, entity: DXFEntity) -> bool:
        """Return True for open entities that commonly represent annotation marks."""
        dxftype = entity.dxftype()
        if dxftype in {'LINE', 'ARC', 'SPLINE'}:
            return True
        if dxftype in {'LWPOLYLINE', 'POLYLINE'}:
            return not self._polyline_is_closed(entity)
        return False

    def _matches_dimension_block_name(self, name: Optional[str]) -> bool:
        """Match anonymous and named dimension blocks."""
        if not name:
            return False

        normalized = str(name).strip().lower()
        if any(normalized.startswith(prefix) for prefix in self.DIMENSION_BLOCK_PREFIXES):
            return True
        return self._matches_annotation_name(normalized)

    def _matches_annotation_name(self, name: Optional[str]) -> bool:
        """Match common dimension/centerline/annotation names by token."""
        if not name:
            return False

        tokens = self._tokenize_name(name)
        return any(token in self.ANNOTATION_NAME_TOKENS for token in tokens)

    def _matches_annotation_linetype_name(self, name: Optional[str]) -> bool:
        """Match common annotation linetypes such as CENTER2 and DIM."""
        if not name:
            return False

        tokens = self._tokenize_name(name)
        if not tokens:
            return False

        for token in tokens:
            if token in self.ANNOTATION_NAME_TOKENS:
                return True
            if any(token.startswith(prefix) for prefix in self.ANNOTATION_LINETYPE_PREFIXES):
                return True
        return False

    def _tokenize_name(self, name: Optional[str]) -> List[str]:
        """Tokenize layer/block/linetype names for conservative keyword matching."""
        if not name:
            return []
        normalized = str(name).strip().lower()
        return [token for token in re.split(r'[^0-9a-z가-힣]+', normalized) if token]

    def _looks_like_breakline_polyline(
        self,
        entity: DXFEntity,
        transform: Optional[Matrix44] = None,
    ) -> bool:
        """
        Detect open zig-zag breaklines placed on symbol/annotation layers.

        Typical breaklines have long lead-in/out segments with a compact
        saw-tooth center; this keeps the heuristic narrower than "any open
        symbol polyline".
        """
        if entity.dxftype() not in {'LWPOLYLINE', 'POLYLINE'}:
            return False

        if self._polyline_is_closed(entity):
            return False

        points = self._get_polyline_points(entity, transform=transform)
        if len(points) < 5:
            return False

        vectors: List[Tuple[float, float]] = []
        lengths: List[float] = []
        for start, end in zip(points, points[1:]):
            dx = end.x - start.x
            dy = end.y - start.y
            length = math.hypot(dx, dy)
            if length <= 1e-6:
                continue
            vectors.append((dx, dy))
            lengths.append(length)

        if len(lengths) < 4:
            return False

        chord = math.hypot(points[-1].x - points[0].x, points[-1].y - points[0].y)
        if chord <= 1e-6:
            return False

        path_to_chord_ratio = sum(lengths) / chord
        if path_to_chord_ratio < 1.08 or path_to_chord_ratio > 3.0:
            return False

        middle_lengths = sorted(lengths[1:-1])
        if not middle_lengths:
            return False

        middle_median = middle_lengths[len(middle_lengths) // 2]
        if lengths[0] < middle_median * 2.0 or lengths[-1] < middle_median * 2.0:
            return False

        sharp_turns = 0
        for previous, current in zip(vectors, vectors[1:]):
            previous_angle = math.degrees(math.atan2(previous[1], previous[0]))
            current_angle = math.degrees(math.atan2(current[1], current[0]))
            turn = abs((current_angle - previous_angle + 180.0) % 360.0 - 180.0)
            if turn >= 45.0:
                sharp_turns += 1

        return sharp_turns >= 2

    def _polyline_is_closed(self, entity: DXFEntity) -> bool:
        """Handle closed-flag access across ezdxf polyline entity variants."""
        if entity.dxftype() == 'LWPOLYLINE':
            return bool(entity.closed)
        if entity.dxftype() == 'POLYLINE':
            return bool(entity.is_closed)
        return False

    def _get_polyline_points(
        self,
        entity: DXFEntity,
        transform: Optional[Matrix44] = None,
    ) -> List[Point]:
        """Return transformed polyline vertices in normalized millimeter units."""
        if entity.dxftype() == 'LWPOLYLINE':
            raw_points = list(entity.vertices_in_wcs())
        else:
            raw_points = [
                (vertex.dxf.location.x, vertex.dxf.location.y)
                for vertex in entity.vertices
            ]

        return [self._apply_transform((point[0], point[1]), transform) for point in raw_points]

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
        transformed_center = self._apply_transform((arc.dxf.center.x, arc.dxf.center.y), transform)
        arc_group_id = self._next_arc_group_id()
        arc_start = None
        arc_end = None

        for i in range(num_segments + 1):
            angle = start_angle + i * angle_step
            x = arc.dxf.center.x + arc.dxf.radius * math.cos(angle)
            y = arc.dxf.center.y + arc.dxf.radius * math.sin(angle)
            curr_point = self._apply_transform((x, y), transform)
            if i == 0:
                arc_start = curr_point
            if i == num_segments:
                arc_end = curr_point

            if prev_point is not None:
                segments.append(Segment(
                    start=prev_point,
                    end=curr_point,
                    meta={
                        'type': 'arc_segment',
                        'layer': arc.dxf.layer,
                        'center': [
                            transformed_center.x,
                            transformed_center.y,
                        ],
                        'radius': arc.dxf.radius * self.unit_scale_to_mm,
                        'sweep_angle_deg': math.degrees(total_angle),
                        'arc_group_id': arc_group_id,
                        'arc_start': [arc_start.x, arc_start.y] if arc_start else None,
                        'arc_end': [curr_point.x, curr_point.y],
                        'source_entity': 'ARC',
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
            logger.debug(f"Failed to process HATCH entity: {e}")

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
        arc_group_id = self._next_arc_group_id()
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
                    'radius': radius,
                    'sweep_angle_deg': abs(math.degrees(end_angle - start_angle)),
                    'arc_group_id': arc_group_id,
                    'arc_start': [start.x, start.y],
                    'arc_end': [end.x, end.y],
                    'source_entity': 'LWPOLYLINE_BULGE',
                }
            ))

            prev_point = curr_point

        return segments

    def _next_arc_group_id(self) -> int:
        self._arc_group_counter += 1
        return self._arc_group_counter

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
