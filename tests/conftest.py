"""Shared pytest fixtures: isolate the cache dir and build a test client."""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Point the cache at a throwaway directory *before* the app is imported.
_TMP = Path(tempfile.mkdtemp(prefix="ccache-test-"))
os.environ["CCACHE_CACHE_DIR"] = str(_TMP / "cache")

from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.main import app, STATS  # noqa: E402


@pytest.fixture(scope="session")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_stats_and_cache(cache_dir):
    """Each test starts with an empty on-disk cache and zeroed counters."""
    if cache_dir.exists():
        for child in cache_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    STATS.update(hits=0, misses=0, failures=0)
    config._invalidation_stats.clear()
    yield


@pytest.fixture
def cache_dir() -> Path:
    return config.CACHE_DIR
