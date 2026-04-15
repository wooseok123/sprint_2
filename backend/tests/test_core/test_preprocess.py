"""
Tests for DXF preprocessing heuristics.
"""
import os
import tempfile

import ezdxf

from core.parser import DXFParser
from core.preprocess import DXFPreprocessor


class TestDXFPreprocessor:
    def _new_doc(self):
        doc = ezdxf.new("R2010", setup=True)
        doc.header["$INSUNITS"] = 4
        return doc

    def test_preprocess_skips_breakline_named_layers(self):
        doc = self._new_doc()
        doc.layers.add("A-WAL")
        doc.layers.add("BREAKLINE")

        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "A-WAL"})
        msp.add_lwpolyline(
            [(20, 20), (30, 30), (40, 10), (50, 30), (60, 20)],
            dxfattribs={"layer": "BREAKLINE"},
        )

        processed = self._preprocess_document(doc)

        assert len(processed.segments) == 1
        assert processed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 100.0,
            "maxY": 0.0,
        }

    def test_preprocess_skips_breakline_shaped_open_polylines_on_symbol_layers(self):
        doc = self._new_doc()
        doc.layers.add("A-WAL")
        doc.layers.add("A-SYM")

        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "A-WAL"})
        msp.add_lwpolyline(
            [(0, 0), (80, 30), (92, 18), (100, 42), (112, 18), (190, 30)],
            dxfattribs={"layer": "A-SYM"},
        )

        processed = self._preprocess_document(doc)

        assert len(processed.segments) == 1
        assert processed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 100.0,
            "maxY": 0.0,
        }

    def test_preprocess_skips_dimension_named_layers(self):
        doc = self._new_doc()
        doc.layers.add("A-WAL")
        doc.layers.add("A-DIM")

        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "A-WAL"})
        msp.add_line((40, -20), (40, 80), dxfattribs={"layer": "A-DIM"})

        processed = self._preprocess_document(doc)

        assert len(processed.segments) == 1
        assert processed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 100.0,
            "maxY": 0.0,
        }

    def test_preprocess_skips_mark_and_text_named_layers(self):
        doc = self._new_doc()
        doc.layers.add("A-WAL")
        doc.layers.add("A-MARK")
        doc.layers.add("A-TEXT")

        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "A-WAL"})
        msp.add_line((40, -20), (40, 80), dxfattribs={"layer": "A-MARK"})
        msp.add_line((60, -20), (60, 80), dxfattribs={"layer": "A-TEXT"})

        processed = self._preprocess_document(doc)

        assert len(processed.segments) == 1
        assert processed.preprocessing["removed_by_annotation"] >= 2
        assert processed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 100.0,
            "maxY": 0.0,
        }

    def test_preprocess_keeps_wall_layer_even_if_text_keyword_is_present(self):
        doc = self._new_doc()
        doc.layers.add("A-WALL-TEXT")

        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "A-WALL-TEXT"})

        processed = self._preprocess_document(doc)

        assert len(processed.segments) == 1
        assert processed.preprocessing["removed_by_annotation"] == 0
        assert processed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 100.0,
            "maxY": 0.0,
        }

    def test_preprocess_removes_dimension_layer_even_without_position_signal(self):
        doc = self._new_doc()
        doc.layers.add("A-WAL")
        doc.layers.add("A-DIM")

        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (1400, 0), (1400, 1000), (0, 1000)], close=True, dxfattribs={"layer": "FRAME"})
        msp.add_lwpolyline([(200, 150), (1200, 150), (1200, 850), (200, 850)], close=True, dxfattribs={"layer": "A-WAL"})
        msp.add_line((500, 250), (500, 550), dxfattribs={"layer": "A-DIM"})

        processed = self._preprocess_document(doc)

        assert processed.preprocessing["removed_by_annotation"] >= 1
        assert not any(
            segment.start.x == 500.0
            and segment.end.x == 500.0
            and {segment.start.y, segment.end.y} == {250.0, 550.0}
            for segment in processed.segments
        )

    def test_preprocess_skips_centerline_linetype_lines(self):
        doc = self._new_doc()
        doc.layers.add("A-WAL")

        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "A-WAL"})
        msp.add_line((50, -20), (50, 120), dxfattribs={"linetype": "CENTER"})

        processed = self._preprocess_document(doc)

        assert len(processed.segments) == 1
        assert processed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 100.0,
            "maxY": 0.0,
        }

    def test_preprocess_skips_centerline_linetype_inherited_from_insert_layer(self):
        doc = self._new_doc()
        doc.layers.add("A-WAL")
        doc.layers.add("A-GRID")

        detail = doc.blocks.new(name="GRID_SEG")
        detail.add_line((0, 0), (0, 120), dxfattribs={"layer": "0", "linetype": "BYBLOCK"})

        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "A-WAL"})
        msp.add_blockref("GRID_SEG", (50, -20), dxfattribs={"layer": "A-GRID", "linetype": "CENTER"})

        processed = self._preprocess_document(doc)

        assert len(processed.segments) == 1
        assert processed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 100.0,
            "maxY": 0.0,
        }

    def test_preprocess_skips_annotation_insert_on_dimension_layer(self):
        doc = self._new_doc()
        doc.layers.add("A-WAL")
        doc.layers.add("A-DIM")

        detail = doc.blocks.new(name="DETAIL_MARK")
        detail.add_line((0, 0), (0, 50))
        detail.add_line((0, 50), (30, 50))

        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "A-WAL"})
        msp.add_blockref("DETAIL_MARK", (20, 20), dxfattribs={"layer": "A-DIM"})

        processed = self._preprocess_document(doc)

        assert len(processed.segments) == 1
        assert processed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 100.0,
            "maxY": 0.0,
        }

    def test_preprocess_removes_title_block_cluster_near_sheet_edge(self):
        doc = self._new_doc()
        msp = doc.modelspace()

        msp.add_lwpolyline([(200, 200), (1200, 200), (1200, 900), (200, 900)], close=True)
        msp.add_lwpolyline([(1500, 0), (1950, 0), (1950, 350), (1500, 350)], close=True)

        for x, y, label in ((1560, 60, "A1"), (1560, 140, "SHEET"), (1560, 220, "NOTE")):
            msp.add_text(label, dxfattribs={"insert": (x, y), "height": 20})

        for y in (80, 120, 160, 200):
            msp.add_line((1700, y), (1900, y))

        processed = self._preprocess_document(doc, run_isolated_segment_cleanup=True)

        assert processed.bbox == {
            "minX": 200.0,
            "minY": 200.0,
            "maxX": 1200.0,
            "maxY": 900.0,
        }
        assert processed.preprocessing["removed_by_title_block"] >= 1

    def test_preprocess_keeps_long_wall_that_only_intersects_title_block_candidate(self):
        doc = self._new_doc()
        msp = doc.modelspace()

        msp.add_lwpolyline([(200, 200), (1200, 200), (1200, 900), (200, 900)], close=True)
        msp.add_line((900, 260), (1800, 260))

        msp.add_lwpolyline([(1500, 0), (1950, 0), (1950, 350), (1500, 350)], close=True)
        for x, y, label in ((1560, 60, "A1"), (1560, 140, "SHEET"), (1560, 220, "NOTE")):
            msp.add_text(label, dxfattribs={"insert": (x, y), "height": 20})
        for y in (80, 120, 160, 200):
            msp.add_line((1700, y), (1900, y))

        processed = self._preprocess_document(doc)

        assert processed.bbox["maxX"] >= 1800.0
        assert processed.preprocessing["title_block_confirmed"] is False
        assert any(
            reason in {"crosses_large_geometry", "no_candidate"}
            for reason in processed.preprocessing["title_block_debug"]["reasons"]
        )

    def test_preprocess_rejects_top_strip_candidate_that_is_too_wide(self):
        doc = self._new_doc()
        msp = doc.modelspace()

        msp.add_lwpolyline([(0, 0), (2000, 0), (2000, 900), (0, 900)], close=True)
        msp.add_line((300, 620), (1700, 620))

        for x, y, label in ((520, 760, "ROOM"), (900, 760, "UNIT"), (1280, 760, "NOTE")):
            msp.add_text(label, dxfattribs={"insert": (x, y), "height": 20})

        for x in (520, 900, 1280):
            msp.add_line((x, 700), (x + 180, 700))
            msp.add_line((x, 660), (x + 180, 660))

        processed = self._preprocess_document(doc)

        assert processed.bbox == {
            "minX": 300.0,
            "minY": 620.0,
            "maxX": 1700.0,
            "maxY": 700.0,
        }
        assert processed.preprocessing["title_block_confirmed"] is False
        assert processed.preprocessing["title_block_candidate_bbox"] is None

    def test_preprocess_rejects_corner_density_that_spreads_across_top_band(self):
        doc = self._new_doc()
        msp = doc.modelspace()

        msp.add_lwpolyline([(0, 0), (2200, 0), (2200, 1200), (0, 1200)], close=True)
        msp.add_line((300, 900), (1900, 900))

        for x in range(320, 1721, 200):
            msp.add_text("N", dxfattribs={"insert": (x, 1120), "height": 18})
            msp.add_line((x, 1080), (x + 90, 1080))

        for y in (880, 940, 1000, 1060):
            msp.add_line((1750, y), (2100, y))
        for x in (1750, 1925, 2100):
            msp.add_line((x, 860), (x, 1080))

        processed = self._preprocess_document(doc)

        assert processed.preprocessing["title_block_candidate_bbox"] is None
        assert processed.preprocessing["title_block_confirmed"] is False

    def test_preprocess_removes_border_frame_polyline(self):
        doc = self._new_doc()
        msp = doc.modelspace()

        msp.add_lwpolyline([(0, 0), (1400, 0), (1400, 1000), (0, 1000)], close=True)
        msp.add_lwpolyline([(200, 200), (1200, 200), (1200, 900), (200, 900)], close=True)

        processed = self._preprocess_document(doc)

        assert processed.bbox == {
            "minX": 200.0,
            "minY": 200.0,
            "maxX": 1200.0,
            "maxY": 900.0,
        }
        assert processed.preprocessing["removed_by_border_frame"] == 1

    def test_preprocess_removes_nested_concentric_polyline_frames(self):
        doc = self._new_doc()
        msp = doc.modelspace()

        for inset in (0, 60, 120):
            msp.add_lwpolyline(
                [(inset, inset), (1400 - inset, inset), (1400 - inset, 1000 - inset), (inset, 1000 - inset)],
                close=True,
            )
        msp.add_lwpolyline([(260, 220), (1140, 220), (1140, 860), (260, 860)], close=True)
        msp.add_line((640, 220), (640, 420))

        processed = self._preprocess_document(doc)

        assert processed.bbox == {
            "minX": 260.0,
            "minY": 220.0,
            "maxX": 1140.0,
            "maxY": 860.0,
        }
        assert processed.preprocessing["removed_by_border_frame"] == 3

    def test_preprocess_removes_4line_outer_frame(self):
        doc = self._new_doc()
        msp = doc.modelspace()

        msp.add_line((0, 0), (1400, 0))
        msp.add_line((1400, 0), (1400, 1000))
        msp.add_line((1400, 1000), (0, 1000))
        msp.add_line((0, 1000), (0, 0))
        msp.add_lwpolyline([(260, 220), (1140, 220), (1140, 860), (260, 860)], close=True)
        msp.add_line((640, 220), (640, 420))

        processed = self._preprocess_document(doc)

        assert processed.bbox == {
            "minX": 260.0,
            "minY": 220.0,
            "maxX": 1140.0,
            "maxY": 860.0,
        }
        assert processed.preprocessing["removed_by_border_frame"] == 4

    def test_preprocess_keeps_frame_candidate_with_only_two_stray_contacts(self):
        doc = self._new_doc()
        msp = doc.modelspace()

        msp.add_lwpolyline([(0, 0), (1400, 0), (1400, 1000), (0, 1000)], close=True)
        msp.add_lwpolyline([(200, 200), (1200, 200), (1200, 900), (200, 900)], close=True)

        msp.add_line((550, 1000), (550, 1180))
        msp.add_line((0, 420), (-120, 420))

        processed = self._preprocess_document(doc)

        assert processed.bbox == {
            "minX": 200.0,
            "minY": 200.0,
            "maxX": 1200.0,
            "maxY": 900.0,
        }
        assert processed.preprocessing["removed_by_border_frame"] == 1

    def test_preprocess_removes_outer_sheet_wrappers_even_when_work_area_is_off_center(self):
        doc = self._new_doc()
        msp = doc.modelspace()

        msp.add_lwpolyline([(0, 0), (2200, 0), (2200, 1600), (0, 1600)], close=True)
        msp.add_lwpolyline([(80, 60), (2120, 60), (2120, 1520), (80, 1520)], close=True)
        msp.add_lwpolyline([(420, 260), (1420, 260), (1420, 980), (420, 980)], close=True)

        msp.add_line((520, 320), (1300, 320))
        msp.add_line((520, 320), (520, 900))
        msp.add_line((520, 900), (1300, 900))
        msp.add_line((1300, 900), (1300, 320))
        msp.add_line((900, 320), (900, 520))
        msp.add_line((900, 720), (1160, 720))

        processed = self._preprocess_document(doc)

        assert processed.bbox == {
            "minX": 520.0,
            "minY": 320.0,
            "maxX": 1300.0,
            "maxY": 900.0,
        }
        assert processed.preprocessing["removed_by_border_frame"] == 3

    def test_preprocess_removes_concentric_frames_outside_work_area_even_with_boundary_contacts(self):
        doc = self._new_doc()
        msp = doc.modelspace()

        for inset in (0, 40, 80, 120):
            msp.add_lwpolyline(
                [(inset, inset), (1000 - inset, inset), (1000 - inset, 800 - inset), (inset, 800 - inset)],
                close=True,
            )
        msp.add_lwpolyline([(200, 200), (800, 200), (800, 600), (200, 600)], close=True)

        for x in (180, 260, 340, 420, 500, 580):
            msp.add_line((x, 120), (x, 170))

        msp.add_line((320, 320), (680, 320))
        msp.add_line((320, 320), (320, 500))

        processed = self._preprocess_document(doc)

        assert processed.bbox == {
            "minX": 320.0,
            "minY": 320.0,
            "maxX": 680.0,
            "maxY": 500.0,
        }
        assert processed.preprocessing["work_area_bbox"] == {
            "minX": 200.0,
            "minY": 200.0,
            "maxX": 800.0,
            "maxY": 600.0,
        }
        assert processed.preprocessing["removed_by_border_frame"] >= 5
        assert not any(
            (
                {segment.start.x, segment.end.x} == {120.0, 880.0}
                and segment.start.y == segment.end.y == 120.0
            )
            or (
                {segment.start.x, segment.end.x} == {120.0, 880.0}
                and segment.start.y == segment.end.y == 680.0
            )
            for segment in processed.segments
        )

    def test_preprocess_keeps_large_outer_rectangle_when_interior_geometry_touches_edges(self):
        doc = self._new_doc()
        msp = doc.modelspace()

        msp.add_lwpolyline([(0, 0), (1400, 0), (1400, 1000), (0, 1000)], close=True)
        msp.add_line((0, 300), (1000, 300))
        msp.add_line((200, 0), (200, 800))
        msp.add_line((1400, 200), (900, 200))
        msp.add_line((1100, 1000), (1100, 500))

        processed = self._preprocess_document(doc)

        assert processed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 1400.0,
            "maxY": 1000.0,
        }
        assert processed.preprocessing["removed_by_border_frame"] == 0

    def test_preprocess_removes_far_isolated_detail_segments(self):
        doc = self._new_doc()
        msp = doc.modelspace()

        msp.add_lwpolyline([(0, 0), (1000, 0), (1000, 800), (0, 800)], close=True)
        msp.add_line((1800, 1700), (1820, 1700))
        msp.add_line((1820, 1700), (1820, 1720))

        processed = self._preprocess_document(doc)

        assert processed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 1000.0,
            "maxY": 800.0,
        }
        assert processed.preprocessing["removed_isolated_segments"] >= 1

    def test_preprocess_removes_floating_single_segment_before_frame_cleanup(self):
        doc = self._new_doc()
        msp = doc.modelspace()

        msp.add_lwpolyline([(0, 0), (1000, 0), (1000, 800), (0, 800)], close=True)
        msp.add_line((320, 300), (380, 300))

        processed = self._preprocess_document(doc, run_isolated_segment_cleanup=False)

        assert processed.preprocessing["removed_floating_segments"] == 1
        assert not any(
            {segment.start.to_2d(), segment.end.to_2d()} == {(320.0, 300.0), (380.0, 300.0)}
            for segment in processed.segments
        )

    def test_preprocess_keeps_t_junction_segment_when_endpoint_hits_other_geometry(self):
        doc = self._new_doc()
        msp = doc.modelspace()

        msp.add_lwpolyline([(0, 0), (1000, 0), (1000, 800), (0, 800)], close=True)
        msp.add_line((500, 0), (500, 220))

        processed = self._preprocess_document(doc, run_isolated_segment_cleanup=False)

        assert processed.preprocessing["removed_floating_segments"] == 0
        assert any(
            {segment.start.to_2d(), segment.end.to_2d()} == {(500.0, 0.0), (500.0, 220.0)}
            for segment in processed.segments
        )

    def test_preprocess_can_defer_isolated_detail_cleanup(self):
        doc = self._new_doc()
        msp = doc.modelspace()

        msp.add_lwpolyline([(0, 0), (1000, 0), (1000, 800), (0, 800)], close=True)
        msp.add_line((1800, 1700), (1820, 1700))
        msp.add_line((1820, 1700), (1820, 1720))

        processed = self._preprocess_document(doc, run_isolated_segment_cleanup=False)

        assert processed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 1820.0,
            "maxY": 1720.0,
        }
        assert processed.preprocessing["removed_isolated_segments"] == 0
        assert processed.preprocessing["isolated_segment_cleanup_deferred"] is True

    def test_preprocess_removes_detached_pure_rectangle(self):
        doc = self._new_doc()
        msp = doc.modelspace()

        msp.add_lwpolyline([(0, 0), (1000, 0), (1000, 800), (0, 800)], close=True)
        msp.add_line((400, 0), (400, 220))
        msp.add_lwpolyline([(1300, 100), (1500, 100), (1500, 260), (1300, 260)], close=True)

        processed = self._preprocess_document(doc)

        assert processed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 1000.0,
            "maxY": 800.0,
        }
        assert processed.preprocessing["segments_after_preprocessing"] > 0

    def test_preprocess_keeps_rectangle_when_other_geometry_touches_boundary(self):
        doc = self._new_doc()
        msp = doc.modelspace()

        msp.add_lwpolyline([(0, 0), (1000, 0), (1000, 800), (0, 800)], close=True)
        msp.add_line((400, 0), (400, 220))

        processed = self._preprocess_document(doc)

        assert processed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 1000.0,
            "maxY": 800.0,
        }
        assert processed.preprocessing["removed_detached_rectangles"] == 0

    def test_preprocess_ignores_dimension_line_when_checking_detached_rectangle_contacts(self):
        doc = self._new_doc()
        doc.layers.add("A-DIM")
        msp = doc.modelspace()

        msp.add_lwpolyline([(0, 0), (1000, 0), (1000, 800), (0, 800)], close=True)
        msp.add_lwpolyline([(1300, 100), (1500, 100), (1500, 260), (1300, 260)], close=True)
        msp.add_line((1300, 180), (1220, 180), dxfattribs={"layer": "A-DIM"})
        msp.add_text("1800", dxfattribs={"insert": (1210, 210), "height": 18, "layer": "A-DIM"})

        processed = self._preprocess_document(doc)

        assert processed.bbox == {
            "minX": 0.0,
            "minY": 0.0,
            "maxX": 1000.0,
            "maxY": 800.0,
        }
        assert processed.preprocessing["removed_detached_rectangles"] == 1
        assert processed.preprocessing["removed_by_annotation"] >= 1

    def _preprocess_document(self, doc, *, run_isolated_segment_cleanup=True):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".dxf", delete=False) as handle:
            temp_path = handle.name
            doc.write(handle)

        try:
            parser = DXFParser(temp_path)
            parsed = parser.parse()
            return DXFPreprocessor(parser).preprocess(
                parsed,
                run_isolated_segment_cleanup=run_isolated_segment_cleanup,
            )
        finally:
            os.unlink(temp_path)
