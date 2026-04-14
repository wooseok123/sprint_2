import os
import sys
import tempfile
from pathlib import Path

import ezdxf

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.cycles import CycleDetector, DetectedCycle
from core.filter import AreaFilter
from core.parser import DXFParser, Point, Segment


def _write_temp_dxf(doc: ezdxf.EzDxf) -> str:
    with tempfile.NamedTemporaryFile(mode='w', suffix='.dxf', delete=False) as temp_file:
        path = temp_file.name
        doc.write(temp_file)
    return path


def test_parser_normalizes_units_to_mm():
    doc = ezdxf.new('R2010', setup=True)
    doc.header['$INSUNITS'] = 6
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (1, 0), (1, 1), (0, 1)], close=True)

    path = _write_temp_dxf(doc)
    try:
        parsed = DXFParser(path).parse()
    finally:
        os.unlink(path)

    assert parsed.insunits_code == 6
    assert parsed.unit_scale_to_mm == 1000.0
    assert parsed.bbox == {'minX': 0.0, 'minY': 0.0, 'maxX': 1000.0, 'maxY': 1000.0}


def test_parser_expands_nested_insert_with_composed_transform():
    doc = ezdxf.new('R2010', setup=True)
    doc.header['$INSUNITS'] = 6

    inner = doc.blocks.new(name='INNER')
    inner.add_line((0, 0), (1, 0))

    outer = doc.blocks.new(name='OUTER')
    outer.add_blockref('INNER', (10, 20))

    doc.modelspace().add_blockref('OUTER', (5, 7))

    path = _write_temp_dxf(doc)
    try:
        parsed = DXFParser(path).parse()
    finally:
        os.unlink(path)

    segment = parsed.segments[0]
    assert (segment.start.x, segment.start.y) == (15000.0, 27000.0)
    assert (segment.end.x, segment.end.y) == (16000.0, 27000.0)


def test_cycle_detector_and_containment_filter_keep_outer_polygon():
    segments = [
        Segment(Point(0, 0), Point(100, 0), {'type': 'line'}),
        Segment(Point(100, 0), Point(100, 100), {'type': 'line'}),
        Segment(Point(100, 100), Point(0, 100), {'type': 'line'}),
        Segment(Point(0, 100), Point(0, 0), {'type': 'line'}),
        Segment(Point(100, 50), Point(120, 50), {'type': 'line'}),
    ]

    cycles = CycleDetector(segments).detect_cycles()
    assert len(cycles) == 1
    assert cycles[0].area == 10000.0

    area_filter = AreaFilter(
        bbox={'minX': 0, 'minY': 0, 'maxX': 100, 'maxY': 100},
        entity_count=2,
    )
    polygons = area_filter.filter_cycles(
        [
            DetectedCycle([(0, 0), (100, 0), (100, 100), (0, 100)], 4, 10000.0, 1.0),
            DetectedCycle([(25, 25), (75, 25), (75, 75), (25, 75)], 4, 2500.0, 1.0),
        ]
    )

    assert len(polygons) == 1
    assert polygons[0].area == 10000.0
