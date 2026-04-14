"""
DXF Boundary Detection API Endpoint
POST /api/detect-boundary

Implements 9-step geometric algorithm pipeline:
1. DXF parsing (ezdxf) + segment normalization
2. Noding (shapely unary_union)
3. Tolerance snapping (KD-tree + Union-Find)
4. Dangling edge pruning
5. Cycle detection (shapely.polygonize)
6. Area filtering (adaptive)
7. Unary union + boundary extraction
8. Validation (simplify, make_valid, HATCH IoU)
"""
import os
import time
import tempfile
from typing import Optional
from fastapi import APIRouter, UploadFile, File, HTTPException, status
from loguru import logger

# Import models
from models.schemas import BoundaryResponse, BoundaryData, Metadata

# Import core algorithm modules
from core.parser import DXFParser
from core.noding import NodingProcessor
from core.graph import GraphProcessor
from core.cycles import CycleDetector
from core.filter import AreaFilter
from core.outline import OutlineExtractorV2
from core.union import BoundaryExtractor
from core.validate import BoundaryValidator

# Create router
router = APIRouter(prefix="/api", tags=["boundary"])

# Configuration from environment
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", 100))
UPLOAD_TIMEOUT_S = int(os.getenv("UPLOAD_TIMEOUT_S", 60))


@router.post("/detect-boundary", response_model=BoundaryResponse)
async def detect_boundary(file: UploadFile = File(...)):
    """
    Detect outer boundary from uploaded DXF file.

    Args:
        file: DXF file upload (multipart/form-data)

    Returns:
        BoundaryResponse with detected boundary coordinates and metadata

    Raises:
        HTTPException 400: Invalid file or DXF format
        HTTPException 413: File too large
        HTTPException 500: Processing error
    """
    start_time = time.time()

    # Validate file extension
    if not file.filename.lower().endswith('.dxf'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file format. Only .dxf files are supported."
        )

    # Check file size (read content to check size)
    content = await file.read()
    file_size_mb = len(content) / (1024 * 1024)

    if file_size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size is {MAX_FILE_SIZE_MB}MB."
        )

    # Reset file pointer for subsequent reads
    await file.seek(0)

    logger.info(f"Processing DXF file: {file.filename} ({file_size_mb:.2f} MB)")

    # Create temporary file for processing
    temp_fd = None
    temp_path = None
    try:
        # Create temp file
        temp_fd, temp_path = tempfile.mkstemp(suffix='.dxf')

        # Write uploaded content to temp file
        with os.fdopen(temp_fd, 'wb') as f:
            f.write(content)

        # ========================================
        # 9-STEP ALGORITHM PIPELINE
        # ========================================
        try:
            # STEP 1: Parse DXF and normalize segments
            logger.info("STEP 1: Parsing DXF file...")
            parser = DXFParser(temp_path)
            parsed = parser.parse()

            logger.info(
                f"Parsed {parsed.entity_count} entities, "
                f"extracted {len(parsed.segments)} segments, "
                f"collected {len(parsed.hatch_entities)} HATCH entities"
            )

            if not parsed.segments:
                return BoundaryResponse(
                    success=False,
                    boundary=None,
                    metadata=None,
                    error="No valid entities found in DXF file. "
                          "Ensure the file contains LINE, LWPOLYLINE, POLYLINE, SPLINE, or ARC entities."
                )

            # STEP 2: Noding (split at intersections)
            logger.info("STEP 2: Applying noding...")
            noding_processor = NodingProcessor(tolerance=0.001)
            noded_segments = noding_processor.apply_noding(parsed.segments)

            # STEP 3-4: Tolerance snapping + graph construction
            logger.info("STEP 3-4: Building graph and snapping endpoints...")
            graph_processor = GraphProcessor(
                bbox=parsed.bbox,
                adaptive_params={
                    'tolerance_global_percent': float(os.getenv("DEFAULT_TOLERANCE_PERCENT", "0.1")),
                    'tolerance_local_percent': 1.0,
                    'min_tolerance_mm': float(os.getenv("MIN_TOLERANCE_MM", "0.001")),
                    'max_tolerance_mm': float(os.getenv("MAX_TOLERANCE_MM", "1.0"))
                }
            )
            graph, snapped_segments = graph_processor.build_graph_and_snap(noded_segments)

            # STEP 5 (auxiliary): prune only short dangling spurs for diagnostics
            logger.info("STEP 5: Pruning short dangling spurs...")
            graph_metrics = graph_processor.prune_dangling_edges(max_iterations=1000)

            logger.info(
                f"Pruning complete: {graph_metrics.pruned_edges} edges removed "
                f"({graph_metrics.pruned_percent:.1f}%), "
                f"{graph_metrics.components} components, "
                f"max degree {graph_metrics.max_degree}"
            )

            # Check for graph anomalies (AI intervention point 1)
            if (graph_metrics.components >= 3 or
                graph_metrics.max_degree >= 6 or
                graph_metrics.pruned_percent > 10):
                logger.warning(
                    f"Graph anomaly detected: components={graph_metrics.components}, "
                    f"max_degree={graph_metrics.max_degree}, "
                    f"pruned_pct={graph_metrics.pruned_percent:.1f}%"
                )
                # TODO: AI judgment integration (requires Gemini API key)
                # For now, proceed with caution

            use_v2 = os.getenv("BOUNDARY_EXTRACTOR_V2", "false").lower() == "true"
            cycles = []

            if use_v2:
                logger.info("STEP 6-8: Extracting outline with OutlineExtractorV2...")
                outline_extractor = OutlineExtractorV2()
                merged_polygon, merge_metadata = outline_extractor.extract_boundary(snapped_segments)
            else:
                # STEP 6: Cycle detection
                logger.info("STEP 6: Detecting cycles...")
                cycle_detector = CycleDetector(snapped_segments)
                cycles = cycle_detector.detect_cycles()

                logger.info(f"Detected {len(cycles)} cycles")

                if not cycles:
                    return BoundaryResponse(
                        success=False,
                        boundary=None,
                        metadata=None,
                        error="No closed cycles detected. The DXF may not form a complete boundary."
                    )

                # Standardize winding direction
                cycles = cycle_detector.standardize_winding(cycles)

                # STEP 7: Area filtering
                logger.info("STEP 7: Filtering by area...")
                area_filter = AreaFilter(
                    bbox=parsed.bbox,
                    entity_count=parsed.entity_count,
                    adaptive_params={
                        'min_area_percent': float(os.getenv("AREA_FILTER_MIN_PERCENT", "0.5")),
                        'max_area_percent': float(os.getenv("AREA_FILTER_MAX_PERCENT", "2.0")),
                        'entity_count_factor': 0.0001,
                        'arc_density_factor': 0.1
                    }
                )

                # Calculate ARC density for adaptive filtering
                arc_segments = sum(1 for seg in parsed.segments if 'arc' in seg.meta.get('type', ''))
                arc_density = arc_segments / len(parsed.segments) if parsed.segments else 0

                valid_polygons = area_filter.filter_cycles(cycles, arc_density=arc_density)

                logger.info(f"Area filter: {len(cycles)} → {len(valid_polygons)} valid polygons")

                if not valid_polygons:
                    return BoundaryResponse(
                        success=False,
                        boundary=None,
                        metadata=None,
                        error="No polygons passed area filter. The detected cycles may be too small."
                    )

                # STEP 8: Unary union + boundary extraction
                logger.info("STEP 8: Extracting boundary...")
                boundary_extractor = BoundaryExtractor()
                merged_polygon, merge_metadata = boundary_extractor.extract_boundary(valid_polygons)

            if merged_polygon is None:
                return BoundaryResponse(
                    success=False,
                    boundary=None,
                    metadata=None,
                    error=f"Failed to extract boundary: {merge_metadata.get('error', 'Unknown error')}"
                )

            # STEP 9: Validation
            logger.info("STEP 9: Validating boundary...")
            validator = BoundaryValidator(simplify_tolerance=0.001)
            validation_result = validator.validate_and_correct(
                merged_polygon,
                hatch_boundaries=parsed.hatch_entities
            )

            if not validation_result.is_valid:
                return BoundaryResponse(
                    success=False,
                    boundary=None,
                    metadata=None,
                    error=validation_result.metadata.get('error', 'Validation failed')
                )

            # Check for validation issues (AI intervention point 3)
            if validator.should_invoke_ai_judge(validation_result):
                logger.warning(
                    f"Validation issues detected: "
                    f"vertices={validation_result.metadata.get('vertex_count', 0)}, "
                    f"confidence={validation_result.metadata['confidence']:.3f}"
                )
                # TODO: AI judgment integration
                # For now, proceed with result

            # Calculate bbox area for metadata
            bbox_area = (parsed.bbox['maxX'] - parsed.bbox['minX']) * \
                       (parsed.bbox['maxY'] - parsed.bbox['minY'])

            # Calculate final processing time
            processing_time_ms = int((time.time() - start_time) * 1000)

            # Prepare metadata
            metadata = Metadata(
                area=validation_result.metadata['area'],
                bbox_area=bbox_area,
                confidence=validation_result.metadata['confidence'],
                cycles_detected=len(cycles),
                processing_time_ms=processing_time_ms,
                entity_count=parsed.entity_count,
                node_count=graph_metrics.node_count,
                edge_count=graph_metrics.edge_count
            )

            # Prepare boundary data
            boundary_data = BoundaryData(
                exterior=validation_result.exterior_coords,
                interiors=validation_result.interiors_coords
            )

            logger.info(
                f"✓ Detection complete: area={metadata.area:.2f}, "
                f"confidence={metadata.confidence:.3f}, "
                f"time={processing_time_ms}ms"
            )

            return BoundaryResponse(
                success=True,
                boundary=boundary_data,
                metadata=metadata,
                error=None
            )

        except Exception as pipeline_error:
            logger.error(f"Pipeline error: {str(pipeline_error)}", exc_info=True)

            return BoundaryResponse(
                success=False,
                boundary=None,
                metadata=None,
                error=f"Pipeline error: {str(pipeline_error)}"
            )

    except Exception as e:
        logger.error(f"Error processing DXF file: {str(e)}", exc_info=True)
        processing_time_ms = int((time.time() - start_time) * 1000)

        return BoundaryResponse(
            success=False,
            boundary=None,
            metadata=None,
            error=f"Processing error: {str(e)}"
        )

    finally:
        # Cleanup temp file
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
                logger.debug(f"Cleaned up temp file: {temp_path}")
            except Exception as e:
                logger.warning(f"Failed to cleanup temp file {temp_path}: {e}")
