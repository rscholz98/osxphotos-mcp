import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def library_path() -> str:
    """Path to osxphotos' own `Test-13.0.0.photoslibrary` (see README, Development)."""
    path = os.environ.get("OSXPHOTOS_MCP_TEST_LIBRARY")
    if not path or not Path(path).exists():
        pytest.skip("set OSXPHOTOS_MCP_TEST_LIBRARY to osxphotos' Test-13.0.0.photoslibrary")
    return path
