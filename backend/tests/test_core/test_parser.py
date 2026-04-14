"""
Tests for DXF parser block expansion behavior.
"""
import os
import tempfile

import ezdxf

from core.parser import DXFParser


class TestDXFParserInsertExpansion:
    """Parser tests focused on INSERT-heavy drawings."""

    def test_parse_expands_modelspace_insert_with_geometry(self):
        """A drawing that only contains an INSERT should still yield segments."""
        doc = ezdxf.new("R2010", setup=True)
        rect_block = doc.blocks.new(name="RECT")
        rect_block.add_lwpolyline([(0, 0), (10, 0), (10, 20), (0, 20)], close=True)

        msp = doc.modelspace()
        msp.add_blockref("RECT", (100, 200))

        parsed = self._parse_document(doc)

        assert parsed.entity_count == 1
        assert len(parsed.segments) == 4
        assert parsed.bbox == {
            "minX": 100.0,
            "minY": 200.0,
            "maxX": 110.0,
            "maxY": 220.0,
        }

    def test_parse_expands_nested_insert_transforms(self):
        """Nested INSERT transforms should compose into final world coordinates."""
        doc = ezdxf.new("R2010", setup=True)
        line_block = doc.blocks.new(name="LINE_SEG")
        line_block.add_line((0, 0), (10, 0))

        wrapper_block = doc.blocks.new(name="WRAP")
        wrapper_insert = wrapper_block.add_blockref("LINE_SEG", (5, 0))
        wrapper_insert.dxf.rotation = 90

        msp = doc.modelspace()
        msp.add_blockref("WRAP", (100, 100))

        parsed = self._parse_document(doc)

        assert len(parsed.segments) == 1
        segment = parsed.segments[0]

        assert round(segment.start.x, 6) == 105.0
        assert round(segment.start.y, 6) == 100.0
        assert round(segment.end.x, 6) == 105.0
        assert round(segment.end.y, 6) == 110.0

    def _parse_document(self, doc):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".dxf", delete=False) as handle:
            temp_path = handle.name
            doc.write(handle)

        try:
            parser = DXFParser(temp_path)
            return parser.parse()
        finally:
            os.unlink(temp_path)
