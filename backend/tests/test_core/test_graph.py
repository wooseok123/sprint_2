import sys
from pathlib import Path

import networkx as nx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import core.graph as graph_module
from core.graph import GraphProcessor
from core.parser import Point, Segment


def _segment(start, end):
    return Segment(Point(*start), Point(*end), {"type": "line"})


def test_graph_processor_extends_dangling_endpoint_to_close_small_gap():
    segments = [
        _segment((0, 0), (1000, 0)),
        _segment((1000, 0), (1000, 600)),
        _segment((1000, 600), (0, 600)),
        _segment((0, 600), (0, 20)),
    ]
    processor = GraphProcessor(
        bbox={"minX": 0, "minY": 0, "maxX": 1000, "maxY": 600},
    )

    graph, snapped_segments = processor.build_graph_and_snap(segments)

    assert processor.extension_metadata["applied_count"] >= 1
    assert processor.extension_metadata["applied_extensions"]
    assert processor.extension_metadata["applied_extensions"][0]["extension_mm"] > 0
    assert nx.number_connected_components(graph) == 1
    assert any(
        segment.start.to_2d() == (0, 0) or segment.end.to_2d() == (0, 0)
        for segment in snapped_segments
    )
    assert all(graph.degree(node) == 2 for node in graph.nodes)


def test_graph_processor_leaves_isolated_segment_without_nearby_target():
    segments = [
        _segment((0, 0), (1000, 0)),
        _segment((2000, 0), (2600, 0)),
    ]
    processor = GraphProcessor(
        bbox={"minX": 0, "minY": 0, "maxX": 2600, "maxY": 0},
    )

    _, snapped_segments = processor.build_graph_and_snap(segments)

    assert processor.extension_metadata["applied_count"] == 0
    assert processor.extension_metadata["applied_extensions"] == []
    assert snapped_segments[0].start.to_2d() == (0, 0)
    assert snapped_segments[0].end.to_2d() == (1000, 0)
    assert snapped_segments[1].start.to_2d() == (2000, 0)
    assert snapped_segments[1].end.to_2d() == (2600, 0)


def test_graph_processor_batches_multiple_gap_closures_in_single_run():
    segments = [
        _segment((0, 0), (1000, 0)),
        _segment((1000, 0), (1000, 600)),
        _segment((1000, 600), (0, 600)),
        _segment((0, 600), (0, 20)),
        _segment((1400, 0), (2400, 0)),
        _segment((2400, 0), (2400, 600)),
        _segment((2400, 600), (1400, 600)),
        _segment((1400, 600), (1400, 20)),
    ]
    processor = GraphProcessor(
        bbox={"minX": 0, "minY": 0, "maxX": 2400, "maxY": 600},
    )

    graph, _ = processor.build_graph_and_snap(segments)

    assert processor.extension_metadata["applied_count"] == 2
    assert len(processor.extension_metadata["applied_extensions"]) == 2
    assert nx.number_connected_components(graph) == 2
    assert sum(1 for degree in dict(graph.degree()).values() if degree == 1) == 0


def test_graph_processor_returns_pruned_active_segments_without_short_spur():
    segments = [
        _segment((0, 0), (1000, 0)),
        _segment((1000, 0), (1000, 600)),
        _segment((1000, 600), (0, 600)),
        _segment((0, 600), (0, 0)),
        _segment((500, 600), (500, 660)),
    ]
    processor = GraphProcessor(
        bbox={"minX": 0, "minY": 0, "maxX": 1000, "maxY": 660},
    )

    _, snapped_segments = processor.build_graph_and_snap(segments)
    metrics = processor.prune_dangling_edges()
    active_segments = processor.get_active_segments(snapped_segments)

    assert metrics.pruned_edges == 1
    assert len(active_segments) == 4
    assert all(
        {
            active.start.to_2d(),
            active.end.to_2d(),
        } != {(500, 600), (500, 660)}
        for active in active_segments
    )


def test_graph_processor_prunes_t_junction_terminal_branch_beyond_short_spur_limit():
    segments = [
        _segment((0, 0), (1000, 0)),
        _segment((1000, 0), (1000, 600)),
        _segment((1000, 600), (500, 600)),
        _segment((500, 600), (0, 600)),
        _segment((0, 600), (0, 0)),
        _segment((500, 600), (500, 840)),
    ]
    processor = GraphProcessor(
        bbox={"minX": 0, "minY": 0, "maxX": 1000, "maxY": 840},
    )

    _, snapped_segments = processor.build_graph_and_snap(segments)
    metrics = processor.prune_dangling_edges()
    active_segments = processor.get_active_segments(snapped_segments)

    assert metrics.pruned_edges == 1
    assert len(active_segments) == 5
    assert all(
        {
            active.start.to_2d(),
            active.end.to_2d(),
        } != {(500, 600), (500, 840)}
        for active in active_segments
    )


def test_graph_processor_prunes_far_small_disconnected_component():
    segments = [
        _segment((0, 0), (1000, 0)),
        _segment((1000, 0), (1000, 800)),
        _segment((1000, 800), (0, 800)),
        _segment((0, 800), (0, 0)),
        _segment((1800, 1700), (1820, 1700)),
        _segment((1820, 1700), (1820, 1720)),
    ]
    processor = GraphProcessor(
        bbox={"minX": 0, "minY": 0, "maxX": 1820, "maxY": 1720},
    )

    _, snapped_segments = processor.build_graph_and_snap(segments)
    metrics = processor.prune_dangling_edges()
    active_segments = processor.get_active_segments(snapped_segments)

    assert metrics.removed_small_components == 1
    assert metrics.removed_small_component_edges == 2
    assert len(active_segments) == 4
    assert all(
        {
            active.start.to_2d(),
            active.end.to_2d(),
        } != {(1800, 1700), (1820, 1700)}
        and {
            active.start.to_2d(),
            active.end.to_2d(),
        } != {(1820, 1700), (1820, 1720)}
        for active in active_segments
    )


def test_graph_processor_snapping_works_without_scipy(monkeypatch):
    monkeypatch.setattr(graph_module, "KDTree", None)

    segments = [
        _segment((0, 0), (1000, 0)),
        _segment((1000.4, 0), (1000.4, 600)),
        _segment((1000, 600), (0, 600)),
        _segment((0, 600), (0, 0)),
    ]
    processor = GraphProcessor(
        bbox={"minX": 0, "minY": 0, "maxX": 1000.4, "maxY": 600},
        adaptive_params={"max_tolerance_mm": 1.0},
    )

    graph, snapped_segments = processor.build_graph_and_snap(segments)

    assert graph.number_of_nodes() == 4
    assert nx.number_connected_components(graph) == 1
    assert snapped_segments[0].end.to_2d() == pytest.approx((1000.2, 0.0), abs=1e-6)
    assert snapped_segments[1].start.to_2d() == pytest.approx((1000.2, 0.0), abs=1e-6)
