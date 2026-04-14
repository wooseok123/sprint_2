"""
Tests for DXF parser block expansion behavior.
"""
import os
import tempfile

import ezdxf

from core.parser import DXFParser


class TestDXFParserInsertExpansion:
    """Parser tests focused on INSERT-heavy drawings."""

    def _new_doc(self):
        doc = ezdxf.new("R2010", setup=True)
        doc.header["$INSUNITS"] = 4
        return doc

    def test_parse_expands_modelspace_insert_with_geometry(self):
        """A drawing that only contains an INSERT should still yield segments."""
        doc = self._new_doc()
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
        doc = self._new_doc()
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

    def test_parse_skips_entities_on_off_layers(self):
        """Entities on layers hidden in CAD should be ignored by the backend too."""
        doc = self._new_doc()
        doc.layers.add("VISIBLE")
        doc.layers.add("HIDDEN")
        doc.layers.get("HIDDEN").off()

        msp = doc.modelspace()
        msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "VISIBLE"})
        msp.add_line((100, 100), (110, 100), dxfattribs={"layer": "HIDDEN"})

        parsed = self._parse_document(doc)

        assert parsed.entity_count == 1
        assert len(parsed.segments) == 1
        assert parsed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 10.0,
            "maxY": 0.0,
        }

    def test_parse_skips_entities_on_frozen_layers(self):
        """Frozen layers should not contribute geometry to boundary detection."""
        doc = self._new_doc()
        doc.layers.add("VISIBLE")
        doc.layers.add("FROZEN")
        doc.layers.get("FROZEN").freeze()

        msp = doc.modelspace()
        msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "VISIBLE"})
        msp.add_line((100, 100), (110, 100), dxfattribs={"layer": "FROZEN"})

        parsed = self._parse_document(doc)

        assert parsed.entity_count == 1
        assert len(parsed.segments) == 1
        assert parsed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 10.0,
            "maxY": 0.0,
        }

    def test_parse_skips_blockrefs_on_off_layers(self):
        """A hidden INSERT should suppress the entire referenced block geometry."""
        doc = self._new_doc()
        doc.layers.add("BLOCK_HIDDEN")
        doc.layers.get("BLOCK_HIDDEN").off()

        rect_block = doc.blocks.new(name="RECT")
        rect_block.add_lwpolyline([(0, 0), (10, 0), (10, 20), (0, 20)], close=True)

        msp = doc.modelspace()
        msp.add_blockref("RECT", (100, 200), dxfattribs={"layer": "BLOCK_HIDDEN"})

        parsed = self._parse_document(doc)

        assert parsed.entity_count == 0
        assert len(parsed.segments) == 0
        assert parsed.bbox == {
            "minX": 0,
            "minY": 0,
            "maxX": 0,
            "maxY": 0,
        }

    def test_parse_skips_breakline_named_layers(self):
        """Common BREAK/BREAKLINE layers should be ignored automatically."""
        doc = self._new_doc()
        doc.layers.add("A-WAL")
        doc.layers.add("BREAKLINE")

        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "A-WAL"})
        msp.add_lwpolyline(
            [(20, 20), (30, 30), (40, 10), (50, 30), (60, 20)],
            dxfattribs={"layer": "BREAKLINE"},
        )

        parsed = self._parse_document(doc)

        assert parsed.entity_count == 1
        assert len(parsed.segments) == 1
        assert parsed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 100.0,
            "maxY": 0.0,
        }

    def test_parse_skips_breakline_shaped_open_polylines_on_symbol_layers(self):
        """Open zig-zag polylines on symbol layers should be treated as breaklines."""
        doc = self._new_doc()
        doc.layers.add("A-WAL")
        doc.layers.add("A-SYM")

        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "A-WAL"})
        msp.add_lwpolyline(
            [(0, 0), (80, 30), (92, 18), (100, 42), (112, 18), (190, 30)],
            dxfattribs={"layer": "A-SYM"},
        )

        parsed = self._parse_document(doc)

        assert parsed.entity_count == 1
        assert len(parsed.segments) == 1
        assert parsed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 100.0,
            "maxY": 0.0,
        }

    def test_parse_skips_dimension_named_layers(self):
        """Open geometry on common DIM layers should be ignored."""
        doc = self._new_doc()
        doc.layers.add("A-WAL")
        doc.layers.add("A-DIM")

        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "A-WAL"})
        msp.add_line((40, -20), (40, 80), dxfattribs={"layer": "A-DIM"})

        parsed = self._parse_document(doc)

        assert parsed.entity_count == 1
        assert len(parsed.segments) == 1
        assert parsed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 100.0,
            "maxY": 0.0,
        }

    def test_parse_skips_centerline_linetype_lines(self):
        """CENTER linetype should be treated as annotation-style linework."""
        doc = self._new_doc()
        doc.layers.add("A-WAL")

        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "A-WAL"})
        msp.add_line((50, -20), (50, 120), dxfattribs={"linetype": "CENTER"})

        parsed = self._parse_document(doc)

        assert parsed.entity_count == 1
        assert len(parsed.segments) == 1
        assert parsed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 100.0,
            "maxY": 0.0,
        }

    def test_parse_skips_annotation_insert_on_dimension_layer(self):
        """INSERTs on DIM layers should suppress their block geometry."""
        doc = self._new_doc()
        doc.layers.add("A-WAL")
        doc.layers.add("A-DIM")

        detail = doc.blocks.new(name="DETAIL_MARK")
        detail.add_line((0, 0), (0, 50))
        detail.add_line((0, 50), (30, 50))

        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "A-WAL"})
        msp.add_blockref("DETAIL_MARK", (20, 20), dxfattribs={"layer": "A-DIM"})

        parsed = self._parse_document(doc)

        assert parsed.entity_count == 1
        assert len(parsed.segments) == 1
        assert parsed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 100.0,
            "maxY": 0.0,
        }

    def test_parse_skips_invisible_entities(self):
        """Entities with the DXF invisible flag should be ignored."""
        doc = self._new_doc()
        msp = doc.modelspace()
        hidden_line = msp.add_line((0, 0), (10, 0))
        hidden_line.dxf.invisible = 1

        parsed = self._parse_document(doc)

        assert parsed.entity_count == 0
        assert len(parsed.segments) == 0
        assert parsed.bbox == {
            "minX": 0,
            "minY": 0,
            "maxX": 0,
            "maxY": 0,
        }

    def test_parse_preserves_symbol_like_linework_for_polygon_stage_cleanup(self):
        """Parser should preserve symbol-like geometry for polygon-stage cleanup."""
        doc = self._new_doc()
        msp = doc.modelspace()

        outer = [(0, 0), (400, 0), (400, 300), (0, 300)]
        inner = [(40, 40), (360, 40), (360, 260), (40, 260)]

        for points in (outer, inner):
            for index, start in enumerate(points):
                end = points[(index + 1) % len(points)]
                msp.add_line(start, end)

        for outer_point, inner_point in zip(outer, inner):
            msp.add_line(outer_point, inner_point)

        door_curve = [
            (140, 60),
            (157, 62),
            (173, 67),
            (188, 76),
            (201, 87),
            (212, 100),
            (221, 115),
            (226, 131),
            (228, 148),
        ]
        msp.add_lwpolyline(door_curve, close=False)

        parsed = self._parse_document(doc)

        assert len(parsed.segments) == 20
        assert parsed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 400.0,
            "maxY": 300.0,
        }

    def _parse_document(self, doc):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".dxf", delete=False) as handle:
            temp_path = handle.name
            doc.write(handle)

        try:
            parser = DXFParser(temp_path)
            return parser.parse()
        finally:
            os.unlink(temp_path)
