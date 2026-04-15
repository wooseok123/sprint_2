"""
Tests for raw DXF parser behavior.
"""
import os
import tempfile

import ezdxf
from ezdxf.math import Matrix44

from core.parser import DXFParser


class TestDXFParserRawExtraction:
    """Parser tests focused on extraction and flattening, not cleanup heuristics."""

    def _new_doc(self):
        doc = ezdxf.new("R2010", setup=True)
        doc.header["$INSUNITS"] = 4
        return doc

    def test_parse_expands_modelspace_insert_with_geometry(self):
        doc = self._new_doc()
        rect_block = doc.blocks.new(name="RECT")
        rect_block.add_lwpolyline([(0, 0), (10, 0), (10, 20), (0, 20)], close=True)

        doc.modelspace().add_blockref("RECT", (100, 200))

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
        doc = self._new_doc()
        line_block = doc.blocks.new(name="LINE_SEG")
        line_block.add_line((0, 0), (10, 0))

        wrapper_block = doc.blocks.new(name="WRAP")
        wrapper_insert = wrapper_block.add_blockref("LINE_SEG", (5, 0))
        wrapper_insert.dxf.rotation = 90

        doc.modelspace().add_blockref("WRAP", (100, 100))

        parsed = self._parse_document(doc)

        assert len(parsed.segments) == 1
        segment = parsed.segments[0]
        assert round(segment.start.x, 6) == 105.0
        assert round(segment.start.y, 6) == 100.0
        assert round(segment.end.x, 6) == 105.0
        assert round(segment.end.y, 6) == 110.0

    def test_parse_skips_geometry_outside_spatially_clipped_insert(self):
        doc = self._new_doc()
        clip_block = doc.blocks.new(name="CLIP_BLOCK")
        clip_block.add_line((0, 0), (10, 0))
        clip_block.add_line((20, 0), (30, 0))

        insert = doc.modelspace().add_blockref("CLIP_BLOCK", (100, 200))
        self._attach_spatial_clip(insert, [(99, 199), (111, 199), (111, 201), (99, 201)])

        parsed = self._parse_document(doc)

        assert parsed.entity_count == 1
        assert len(parsed.segments) == 1
        assert parsed.bbox == {
            "minX": 100.0,
            "minY": 200.0,
            "maxX": 110.0,
            "maxY": 200.0,
        }

    def test_parse_clips_geometry_crossing_spatially_clipped_insert_boundary(self):
        doc = self._new_doc()
        clip_block = doc.blocks.new(name="PARTIAL_CLIP_BLOCK")
        clip_block.add_line((0, 0), (20, 0))

        insert = doc.modelspace().add_blockref("PARTIAL_CLIP_BLOCK", (100, 200))
        self._attach_spatial_clip(insert, [(100, 199), (110, 199), (110, 201), (100, 201)])

        parsed = self._parse_document(doc)

        assert len(parsed.segments) == 1
        segment = parsed.segments[0]
        assert segment.start.x == 100.0
        assert segment.start.y == 200.0
        assert segment.end.x == 110.0
        assert segment.end.y == 200.0

    def test_parse_skips_entities_on_off_layers(self):
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
        doc = self._new_doc()
        doc.layers.add("BLOCK_HIDDEN")
        doc.layers.get("BLOCK_HIDDEN").off()

        rect_block = doc.blocks.new(name="RECT")
        rect_block.add_lwpolyline([(0, 0), (10, 0), (10, 20), (0, 20)], close=True)

        doc.modelspace().add_blockref("RECT", (100, 200), dxfattribs={"layer": "BLOCK_HIDDEN"})

        parsed = self._parse_document(doc)

        assert parsed.entity_count == 0
        assert len(parsed.segments) == 0
        assert parsed.bbox == {
            "minX": 0,
            "minY": 0,
            "maxX": 0,
            "maxY": 0,
        }

    def test_parse_keeps_annotation_like_geometry_for_optional_preprocessing(self):
        doc = self._new_doc()
        doc.layers.add("A-WAL")
        doc.layers.add("A-DIM")
        doc.layers.add("BREAKLINE")

        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "A-WAL"})
        msp.add_line((40, -20), (40, 80), dxfattribs={"layer": "A-DIM"})
        msp.add_line((50, -20), (50, 120), dxfattribs={"linetype": "CENTER"})
        msp.add_lwpolyline(
            [(20, 20), (30, 30), (40, 10), (50, 30), (60, 20)],
            dxfattribs={"layer": "BREAKLINE"},
        )

        parsed = self._parse_document(doc)

        assert parsed.entity_count == 4
        assert len(parsed.segments) == 7
        assert parsed.bbox == {
            "minX": 0.0,
            "minY": -20.0,
            "maxX": 100.0,
            "maxY": 120.0,
        }

    def test_parse_exposes_non_geometric_entities_to_preprocessor(self):
        doc = self._new_doc()
        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0))
        msp.add_text("NOTE", dxfattribs={"insert": (200, 100), "height": 10})

        parsed = self._parse_document(doc)

        flattened_types = [item.entity.dxftype() for item in parsed.flattened_entities]
        assert flattened_types.count("LINE") == 1
        assert flattened_types.count("TEXT") == 1
        assert len(parsed.segments) == 1

    def test_parse_can_skip_segment_build_for_preview_only_flows(self):
        doc = self._new_doc()
        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0))
        msp.add_text("NOTE", dxfattribs={"insert": (200, 100), "height": 10})

        with tempfile.NamedTemporaryFile(mode="w", suffix=".dxf", delete=False) as handle:
            temp_path = handle.name
            doc.write(handle)

        try:
            parser = DXFParser(temp_path)
            parsed = parser.parse(build_segments=False, build_hatches=False)
        finally:
            os.unlink(temp_path)

        flattened_types = [item.entity.dxftype() for item in parsed.flattened_entities]
        assert flattened_types.count("LINE") == 1
        assert flattened_types.count("TEXT") == 1
        assert parsed.segments == []
        assert parsed.hatch_entities == []
        assert parsed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 200.0,
            "maxY": 100.0,
        }

    def test_parse_skips_invisible_entities(self):
        doc = self._new_doc()
        hidden_line = doc.modelspace().add_line((0, 0), (10, 0))
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

    def _parse_document(self, doc):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".dxf", delete=False) as handle:
            temp_path = handle.name
            doc.write(handle)

        try:
            parser = DXFParser(temp_path)
            return parser.parse()
        finally:
            os.unlink(temp_path)

    def _attach_spatial_clip(self, insert, boundary_vertices):
        extension_dict = insert.new_extension_dict()
        acad_filter = insert.doc.objects.add_dictionary(owner=extension_dict.dictionary.dxf.handle, hard_owned=True)
        extension_dict["ACAD_FILTER"] = acad_filter

        spatial_filter = insert.doc.objects.new_entity("SPATIAL_FILTER", dxfattribs={"owner": acad_filter.dxf.handle})
        spatial_filter.set_boundary_vertices(boundary_vertices)

        inverse_insert_matrix = insert.matrix44()
        inverse_insert_matrix.inverse()
        spatial_filter.set_inverse_insert_matrix(inverse_insert_matrix)
        spatial_filter.set_transform_matrix(Matrix44())

        acad_filter["SPATIAL"] = spatial_filter
