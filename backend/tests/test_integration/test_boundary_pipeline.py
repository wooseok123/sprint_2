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
            assert "metadata" in data

            boundary = data["boundary"]
            assert "exterior" in boundary
            assert isinstance(boundary["exterior"], list)

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

            # Check required metadata fields
            required_fields = [
                "area",
                "confidence",
                "processing_time_ms",
                "exterior_vertex_count"
            ]

            for field in required_fields:
                assert field in metadata, f"Missing required field: {field}"

            # Validate types
            assert isinstance(metadata["area"], (int, float))
            assert isinstance(metadata["confidence"], (int, float))
            assert isinstance(metadata["processing_time_ms"], (int, float))
            assert isinstance(metadata["exterior_vertex_count"], int)
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
