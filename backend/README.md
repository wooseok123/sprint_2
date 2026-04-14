# DXF Boundary Detection Backend

FastAPI backend for detecting outer boundaries from DXF files using geometric algorithms.

## Quick Start

```bash
# 1. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env and add your GEMINI_API_KEY

# 4. Run server
uvicorn main:app --reload
```

## API Endpoints

- `GET /` - API info
- `GET /health` - Health check
- `POST /api/detect-boundary` - Detect boundary from DXF file
- `GET /docs` - Swagger UI documentation

## Configuration

Edit `.env` file:

```bash
# Required for AI judgment features
GEMINI_API_KEY=your_actual_gemini_api_key_here

# Optional settings
MAX_FILE_SIZE_MB=100
UPLOAD_TIMEOUT_S=60
DEFAULT_TOLERANCE_PERCENT=0.1
```

## Project Structure

```
backend/
├── api/
│   └── detect_boundary.py    # Upload endpoint
├── core/
│   ├── parser.py              # DXF parsing (TODO)
│   ├── segment.py             # Segment normalization (TODO)
│   ├── noding.py              # Shapely noding (TODO)
│   ├── graph.py               # Graph operations (TODO)
│   ├── cycles.py              # Cycle detection (TODO)
│   ├── filter.py              # Area filtering (TODO)
│   ├── union.py               # Boundary union (TODO)
│   ├── validate.py            # Validation (TODO)
│   └── ai_judge.py            # AI intervention (TODO)
├── models/
│   └── schemas.py             # Pydantic models
├── tests/
│   └── test_api/
│       └── test_upload.py     # API tests
├── main.py                    # FastAPI app
├── requirements.txt           # Dependencies
└── .env.example               # Environment template
```

## Current Status

✅ **Phase 1 Complete:** Backend API Foundation
- FastAPI server setup
- File upload endpoint
- Pydantic models
- CORS configuration
- Basic tests

🚧 **Phase 2 In Progress:** Core Algorithm Implementation (9-step pipeline)

## Testing

```bash
# Run tests
pytest tests/test_api/test_upload.py -v

# Manual test with curl
curl -X POST "http://localhost:8000/api/detect-boundary" \
  -F "file=@test.dxf"
```

## Technology Stack

- **FastAPI** - Web framework
- **ezdxf** - DXF parsing
- **shapely** - Geometric operations
- **scipy** - Spatial search (KD-tree)
- **networkx** - Graph algorithms
- **google-generativeai** - Gemini AI for judgment

## See Also

- [Progress Documentation](../docs/progress.md)
- [Implementation Plan](../docs/plans/2026-04-14-001-feat-dxf-outer-boundary-detection-plan.md)
