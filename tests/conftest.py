"""Shared pytest fixtures for yuxu tests."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def bundled_dir() -> str:
    """Absolute path to the installed yuxu.bundled package (shipped agents)."""
    import yuxu.bundled
    return str(Path(yuxu.bundled.__file__).parent)
