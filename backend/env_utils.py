"""
Utilities for loading backend-local environment variables.
"""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


BACKEND_ROOT = Path(__file__).resolve().parent
BACKEND_ENV_PATH = BACKEND_ROOT / ".env"


def load_backend_env() -> bool:
    """Load `backend/.env` if it exists and return whether it was found."""
    if not BACKEND_ENV_PATH.exists():
        return False
    return load_dotenv(BACKEND_ENV_PATH, override=True)
