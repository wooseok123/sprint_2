"""
Tests for File Upload API
"""
import pytest
import io
from fastapi.testclient import TestClient
from main import app


# Create test client
client = TestClient(app)


class TestFileUploadEndpoint:
    """Test suite for /api/detect-boundary endpoint."""

    def test_health_check(self):
        """Test that API is running."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

    def test_root_endpoint(self):
        """Test root endpoint returns API info."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        assert "endpoints" in data

    def test_upload_non_dxf_file(self):
        """Test uploading non-DXF file returns 400."""
        # Create a fake text file
        file_content = b"This is not a DXF file"
        files = {"file": ("test.txt", io.BytesIO(file_content), "text/plain")}

        response = client.post("/api/detect-boundary", files=files)

        assert response.status_code == 400
        assert "Invalid file format" in response.json()["detail"]

    def test_preprocess_upload_non_dxf_file(self):
        """Test uploading non-DXF file to preprocess endpoint returns 400."""
        file_content = b"This is not a DXF file"
        files = {"file": ("test.txt", io.BytesIO(file_content), "text/plain")}

        response = client.post("/api/preprocess-dxf", files=files)

        assert response.status_code == 400
        assert "Invalid file format" in response.json()["detail"]

    def test_upload_empty_file(self):
        """Test uploading empty file."""
        file_content = b""
        files = {"file": ("empty.dxf", io.BytesIO(file_content), "application/dxf")}

        response = client.post("/api/detect-boundary", files=files)

        # Should return 200 with error message (not HTTP exception)
        # because processing catches the error
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "error" in data

    def test_upload_invalid_dxf_content(self):
        """Test uploading invalid DXF file content."""
        file_content = b"This is not valid DXF content"
        files = {"file": ("test.dxf", io.BytesIO(file_content), "application/dxf")}

        response = client.post("/api/detect-boundary", files=files)

        # Should return 200 with error (processing catches the error)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "error" in data

    def test_cors_headers(self):
        """Test CORS headers are present."""
        response = client.options("/api/detect-boundary")
        # CORS middleware should add headers
        assert "access-control-allow-origin" in response.headers


# To run tests: pytest backend/tests/test_api/test_upload.py
# Make sure to install: pip install pytest httpx
