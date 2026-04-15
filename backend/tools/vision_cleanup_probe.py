#!/usr/bin/env python3
"""
Generate debug artifacts for the experimental Gemini vision cleanup stage.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.graph import GraphProcessor
from core.noding import NodingProcessor
from core.outline import OutlineExtractorV2
from core.outline_cv_fallback import apply_cv_door_fallback, collect_cv_candidate_bounds
from core.parser import DXFParser
from core.preprocess import DXFPreprocessor
from core.validate import BoundaryValidator
from core.vision_cleanup import (
    collect_vision_cleanup_candidates,
    maybe_apply_gemini_vision_cleanup,
    render_candidate_overlay_png,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf_path", type=Path, help="Path to a DXF file to inspect")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "tmp" / "vision_cleanup_probe",
        help="Directory to write JSON and PNG artifacts into",
    )
    parser.add_argument(
        "--run-gemini",
        action="store_true",
        help="Actually call Gemini using GEMINI_API_KEY and write final cleanup metadata",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dxf_path = args.dxf_path.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    parser = DXFParser(str(dxf_path))
    parsed = parser.parse()
    preprocessed = DXFPreprocessor(parser).preprocess(parsed)

    noding = NodingProcessor(tolerance=0.001)
    noded_segments = noding.apply_noding(preprocessed.segments)

    graph_processor = GraphProcessor(
        bbox=preprocessed.bbox,
        adaptive_params={
            "tolerance_global_percent": 0.1,
            "tolerance_local_percent": 1.0,
            "min_tolerance_mm": 0.001,
            "max_tolerance_mm": 1.0,
        },
    )
    _, snapped_segments = graph_processor.build_graph_and_snap(noded_segments)
    graph_metrics = graph_processor.prune_dangling_edges(max_iterations=1000)
    pruned_segments = graph_processor.get_active_segments(snapped_segments) or snapped_segments

    extractor = OutlineExtractorV2()
    polygon, outline_metadata = extractor.extract_boundary(pruned_segments)
    if polygon is None:
        raise SystemExit(f"outline extraction failed: {outline_metadata}")

    preferred_bounds = (
        tuple(outline_metadata["bridge_last_candidate_bounds"])
        if outline_metadata.get("bridge_last_candidate_bounds")
        else None
    )

    cv_bounds = collect_cv_candidate_bounds(
        polygon=polygon,
        parsed_segments=parsed.segments,
        wall_thickness=outline_metadata.get("estimated_wall_thickness", 0.0),
        preferred_bounds=preferred_bounds,
    )
    cv_attempts = []
    for index, bounds in enumerate(cv_bounds):
        attempt_polygon, attempt_meta = apply_cv_door_fallback(
            polygon=polygon,
            parsed_segments=parsed.segments,
            candidate_bounds=tuple(bounds),
            wall_thickness=outline_metadata.get("estimated_wall_thickness", 0.0),
        )
        cv_attempts.append(
            {
                "index": index,
                "bounds": list(bounds),
                "applied": attempt_polygon is not None,
                "metadata": attempt_meta,
            }
        )

    vision_candidates = collect_vision_cleanup_candidates(
        polygon=polygon,
        reference_segments=preprocessed.segments,
        wall_thickness=outline_metadata.get("estimated_wall_thickness", 0.0),
        preferred_bounds=preferred_bounds,
    )
    candidate_summaries = []
    for index, candidate in enumerate(vision_candidates):
        image_bytes, render_meta = render_candidate_overlay_png(
            polygon=polygon,
            reference_segments=preprocessed.segments,
            candidate=candidate,
            wall_thickness=outline_metadata.get("estimated_wall_thickness", 0.0),
        )
        image_path = output_dir / f"candidate_{index:02d}_{candidate.kind}.png"
        image_path.write_bytes(image_bytes)
        candidate_summaries.append(
            {
                "index": index,
                "kind": candidate.kind,
                "source": candidate.source,
                "bounds": list(candidate.bounds),
                "image_path": str(image_path),
                "render": render_meta,
            }
        )

    gemini_cleanup = None
    validated = BoundaryValidator(simplify_tolerance=0.001).validate_and_correct(
        polygon,
        hatch_boundaries=parsed.hatch_entities,
    )
    if args.run_gemini:
        cleaned_polygon, gemini_cleanup = maybe_apply_gemini_vision_cleanup(
            polygon=polygon,
            reference_segments=preprocessed.segments,
            wall_thickness=outline_metadata.get("estimated_wall_thickness", 0.0),
            preferred_bounds=preferred_bounds,
        )
        validated = BoundaryValidator(simplify_tolerance=0.001).validate_and_correct(
            cleaned_polygon,
            hatch_boundaries=parsed.hatch_entities,
        )

    report = {
        "dxf_path": str(dxf_path),
        "graph_pruning": {
            "pruned_edges": graph_metrics.pruned_edges,
            "pruned_percent": graph_metrics.pruned_percent,
            "components": graph_metrics.components,
            "max_degree": graph_metrics.max_degree,
            "removed_small_components": graph_metrics.removed_small_components,
            "removed_small_component_edges": graph_metrics.removed_small_component_edges,
            "outline_input_segments": len(pruned_segments),
        },
        "outline_extraction": outline_metadata,
        "cv_attempts": cv_attempts,
        "vision_candidates": candidate_summaries,
        "gemini_cleanup": gemini_cleanup,
        "validated_boundary": {
            "is_valid": validated.is_valid,
            "vertex_count": len(validated.exterior_coords),
            "area": validated.metadata.get("area"),
            "confidence": validated.metadata.get("confidence"),
        },
    }
    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
