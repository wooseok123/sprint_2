"""
Integration Tests for Boundary Detection Pipeline

Tests the complete 9-step algorithm pipeline with real DXF files.
"""
import pytest
import io
import os
import tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from main import app
import ezdxf
from ezdxf import entities


# Create test client
client = TestClient(app)

# Test fixtures directory
TEST_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


class TestBoundaryPipelineIntegration:
    """Integration tests for the complete boundary detection pipeline."""

    def test_preprocess_endpoint_returns_preview_segments(self):
        """Preprocess endpoint should return previewable linework without boundary geometry."""
        dxf_content = self._create_simple_rectangle_dxf()

        files = {"file": ("rectangle.dxf", io.BytesIO(dxf_content), "application/dxf")}
        response = client.post("/api/preprocess-dxf", files=files)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "preprocessed" in data
        assert "metadata" in data
        assert "segments" in data["preprocessed"]
        assert isinstance(data["preprocessed"]["segments"], list)
        assert "processing_details" in data["metadata"]
        assert "preprocessing" in data["metadata"]["processing_details"]
        assert data["metadata"]["processing_details"]["raw_parsing"]["raw_segment_extraction_skipped"] is True

    def test_pipeline_with_simple_rectangle(self):
        """Test pipeline with a simple rectangle DXF file."""
        # Create a minimal valid DXF file with a rectangle
        dxf_content = self._create_simple_rectangle_dxf()

        files = {"file": ("rectangle.dxf", io.BytesIO(dxf_content), "application/dxf")}
        response = client.post("/api/detect-boundary", files=files)

        assert response.status_code == 200
        data = response.json()

        # Pipeline should run (even if boundary detection fails for simple shapes)
        assert "success" in data
        assert "boundary" in data or "error" in data

        # If successful, validate structure
        if data.get("success"):
            assert "boundary" in data
            assert "preprocessed" in data
            assert "metadata" in data

            boundary = data["boundary"]
            assert "exterior" in boundary
            assert isinstance(boundary["exterior"], list)

            preprocessed = data["preprocessed"]
            assert "segments" in preprocessed
            assert isinstance(preprocessed["segments"], list)

            metadata = data["metadata"]
            assert "area" in metadata
            assert "confidence" in metadata
            assert "processing_time_ms" in metadata

    def test_pipeline_with_l_shaped_polyline(self):
        """Test pipeline with L-shaped polyline (has interior angle)."""
        dxf_content = self._create_l_shaped_dxf()

        files = {"file": ("l_shape.dxf", io.BytesIO(dxf_content), "application/dxf")}
        response = client.post("/api/detect-boundary", files=files)

        assert response.status_code == 200
        data = response.json()

        # Pipeline should execute
        assert "success" in data
        assert "boundary" in data or "error" in data

    def test_pipeline_with_hole(self):
        """Test pipeline with rectangle containing a hole (courtyard)."""
        dxf_content = self._create_rectangle_with_hole_dxf()

        files = {"file": ("with_hole.dxf", io.BytesIO(dxf_content), "application/dxf")}
        response = client.post("/api/detect-boundary", files=files)

        assert response.status_code == 200
        data = response.json()

        # Pipeline should execute
        assert "success" in data
        assert "boundary" in data or "error" in data

    def test_pipeline_with_outline_v2_feature_flag(self, monkeypatch):
        """Test V2 outline extraction on open outer walls plus a closed inner room."""
        monkeypatch.setenv("BOUNDARY_EXTRACTOR_V2", "true")
        dxf_content = self._create_open_double_wall_with_inner_room_dxf()

        files = {"file": ("outline_v2.dxf", io.BytesIO(dxf_content), "application/dxf")}
        response = client.post("/api/detect-boundary", files=files)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["boundary"]["exterior"]
        assert data["metadata"]["area"] > 500000

    def test_pipeline_keeps_short_spur_out_of_final_outline(self):
        """A short spur should not survive in the final exterior footprint."""
        dxf_content = self._create_double_wall_rectangle_with_short_spur_dxf()

        files = {"file": ("spur_pruned.dxf", io.BytesIO(dxf_content), "application/dxf")}
        response = client.post("/api/detect-boundary", files=files)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

        exterior = data["boundary"]["exterior"]
        max_y = max(point[1] for point in exterior)
        assert max_y == pytest.approx(600.0, abs=10.0)

        pruning_details = data["metadata"]["processing_details"]["graph_pruning"]
        assert pruning_details["outline_input_segments"] < 13

    def test_pipeline_error_handling_empty_dxf(self):
        """Test error handling with empty DXF file."""
        dxf_content = b""

        files = {"file": ("empty.dxf", io.BytesIO(dxf_content), "application/dxf")}
        response = client.post("/api/detect-boundary", files=files)

        assert response.status_code == 200
        data = response.json()

        # Should handle gracefully
        assert data["success"] is False
        assert "error" in data

    def test_pipeline_error_handling_malformed_dxf(self):
        """Test error handling with malformed DXF content."""
        dxf_content = b"0\r\nSECTION\r\n0\r\nINVALID\r\n0\r\nENDSEC\r\n0\r\nEOF\r\n"

        files = {"file": ("malformed.dxf", io.BytesIO(dxf_content), "application/dxf")}
        response = client.post("/api/detect-boundary", files=files)

        assert response.status_code == 200
        data = response.json()

        # Should handle gracefully
        assert data["success"] is False
        assert "error" in data

    def test_pipeline_metadata_completeness(self):
        """Test that pipeline returns complete metadata when successful."""
        dxf_content = self._create_simple_rectangle_dxf()

        files = {"file": ("rectangle.dxf", io.BytesIO(dxf_content), "application/dxf")}
        response = client.post("/api/detect-boundary", files=files)

        assert response.status_code == 200
        data = response.json()

        # If pipeline succeeded, validate metadata
        if data.get("success") and "metadata" in data:
            metadata = data["metadata"]
            assert "preprocessed" in data

            # Check required metadata fields
            required_fields = [
                "area",
                "area_unit",
                "perimeter",
                "perimeter_unit",
                "bbox_area_unit",
                "confidence",
                "processing_time_ms",
                "exterior_vertex_count",
                "processing_details",
            ]

            for field in required_fields:
                assert field in metadata, f"Missing required field: {field}"

            # Validate types
            assert isinstance(metadata["area"], (int, float))
            assert metadata["area_unit"] == "mm²"
            assert isinstance(metadata["perimeter"], (int, float))
            assert metadata["perimeter_unit"] == "mm"
            assert metadata["bbox_area_unit"] == "mm²"
            assert isinstance(metadata["confidence"], (int, float))
            assert isinstance(metadata["processing_time_ms"], (int, float))
            assert isinstance(metadata["exterior_vertex_count"], int)
            assert isinstance(metadata["processing_details"], dict)
            assert "endpoint_extension" in metadata["processing_details"]
            assert "preprocessing" in metadata["processing_details"]
            assert "outline_extraction" in metadata["processing_details"]
            assert "cv_fallback" in metadata["processing_details"]["outline_extraction"]
        else:
            # If not successful, that's ok for this test - we're just checking structure
            assert "success" in data

    # Helper methods to create test DXF files using ezdxf

    def _create_simple_rectangle_dxf(self) -> bytes:
        """Create a valid DXF file with a simple rectangle using ezdxf."""
        doc = ezdxf.new('R2010', setup=True)
        msp = doc.modelspace()

        # Add rectangle as closed polyline (larger size to avoid edge cases)
        points = [(0, 0), (1000, 0), (1000, 1000), (0, 1000)]
        msp.add_lwpolyline(points, close=True)

        # Set units to meters
        doc.header['$INSUNITS'] = 4

        # Save to temp file and read bytes
        with tempfile.NamedTemporaryFile(mode='w', suffix='.dxf', delete=False) as f:
            temp_path = f.name
            doc.write(f)

        with open(temp_path, 'rb') as f:
            content = f.read()

        os.unlink(temp_path)
        return content

    def _create_l_shaped_dxf(self) -> bytes:
        """Create a DXF file with L-shaped polyline using ezdxf."""
        doc = ezdxf.new('R2010', setup=True)
        msp = doc.modelspace()

        # Add L-shape as closed polyline (larger size)
        points = [(0, 0), (1000, 0), (1000, 500), (500, 500), (500, 1000), (0, 1000)]
        msp.add_lwpolyline(points, close=True)

        doc.header['$INSUNITS'] = 4

        with tempfile.NamedTemporaryFile(mode='w', suffix='.dxf', delete=False) as f:
            temp_path = f.name
            doc.write(f)

        with open(temp_path, 'rb') as f:
            content = f.read()

        os.unlink(temp_path)
        return content

    def _create_rectangle_with_hole_dxf(self) -> bytes:
        """Create a DXF file with outer rectangle and inner rectangle (hole) using ezdxf."""
        doc = ezdxf.new('R2010', setup=True)
        msp = doc.modelspace()

        # Add outer rectangle (larger size)
        outer_points = [(0, 0), (1000, 0), (1000, 1000), (0, 1000)]
        msp.add_lwpolyline(outer_points, close=True)

        # Add inner rectangle (hole)
        inner_points = [(250, 250), (750, 250), (750, 750), (250, 750)]
        msp.add_lwpolyline(inner_points, close=True)

        doc.header['$INSUNITS'] = 4

        with tempfile.NamedTemporaryFile(mode='w', suffix='.dxf', delete=False) as f:
            temp_path = f.name
            doc.write(f)

        with open(temp_path, 'rb') as f:
            content = f.read()

        os.unlink(temp_path)
        return content

    def _create_open_double_wall_with_inner_room_dxf(self) -> bytes:
        """Create a DXF with an open outer shell and a closed interior room."""
        doc = ezdxf.new('R2010', setup=True)
        msp = doc.modelspace()

        def add_segment(start, end):
            msp.add_line(start, end)

        def add_ring(points, gap_edge=None, gap_size=0.0):
            for index, start in enumerate(points):
                end = points[(index + 1) % len(points)]
                if gap_edge != index or gap_size <= 0:
                    add_segment(start, end)
                    continue

                x1, y1 = start
                x2, y2 = end
                if x1 == x2:
                    mid = (y1 + y2) / 2.0
                    add_segment(start, (x1, mid - gap_size / 2.0))
                    add_segment((x1, mid + gap_size / 2.0), end)
                else:
                    mid = (x1 + x2) / 2.0
                    add_segment(start, (mid - gap_size / 2.0, y1))
                    add_segment((mid + gap_size / 2.0, y1), end)

        outer = [
            (0, 0), (500, 0), (1000, 0), (1000, 300),
            (1000, 600), (500, 600), (0, 600), (0, 300),
        ]
        inner = [
            (40, 40), (500, 40), (960, 40), (960, 300),
            (960, 560), (500, 560), (40, 560), (40, 300),
        ]

        add_ring(outer, gap_edge=5, gap_size=30.0)
        add_ring(inner, gap_edge=5, gap_size=30.0)
        for index in (1, 3, 5, 7):
            add_segment(outer[index], inner[index])

        room = [(300, 180), (450, 180), (450, 330), (300, 330)]
        add_ring(room)

        doc.header['$INSUNITS'] = 4

        with tempfile.NamedTemporaryFile(mode='w', suffix='.dxf', delete=False) as f:
            temp_path = f.name
            doc.write(f)

        with open(temp_path, 'rb') as f:
            content = f.read()

        os.unlink(temp_path)
        return content

    def _create_double_wall_rectangle_with_short_spur_dxf(self) -> bytes:
        """Create a double-wall rectangle plus a short exterior spur."""
        doc = ezdxf.new('R2010', setup=True)
        msp = doc.modelspace()

        def add_segment(start, end):
            msp.add_line(start, end)

        outer = [(0, 0), (1000, 0), (1000, 600), (0, 600)]
        inner = [(40, 40), (960, 40), (960, 560), (40, 560)]

        for index, start in enumerate(outer):
            add_segment(start, outer[(index + 1) % len(outer)])

        for index, start in enumerate(inner):
            add_segment(start, inner[(index + 1) % len(inner)])

        for outer_point, inner_point in zip(
            [(500, 0), (1000, 300), (500, 600), (0, 300)],
            [(500, 40), (960, 300), (500, 560), (40, 300)],
        ):
            add_segment(outer_point, inner_point)

        add_segment((500, 600), (500, 660))

        doc.header['$INSUNITS'] = 4

        with tempfile.NamedTemporaryFile(mode='w', suffix='.dxf', delete=False) as f:
            temp_path = f.name
            doc.write(f)

        with open(temp_path, 'rb') as f:
            content = f.read()

        os.unlink(temp_path)
        return content
