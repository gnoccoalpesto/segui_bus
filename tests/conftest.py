"""Fixtures shared by the test suite."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from gtfs_sample import write_sample_zip

from segui_bus.config import load_settings
from segui_bus.domain import StaticFeed
from segui_bus.static_gtfs import parse_gtfs_zip


@pytest.fixture
def settings(tmp_path: Path):
    """Real settings, pointed at a throwaway cache directory."""
    return replace(load_settings(), cache_dir=tmp_path / "cache")


@pytest.fixture
def static_zip(tmp_path: Path) -> Path:
    """The synthetic gtfs.zip, written to a temp directory."""
    return write_sample_zip(tmp_path / "gtfs.zip")


@pytest.fixture
def static(static_zip: Path) -> StaticFeed:
    """The synthetic feed, already parsed."""
    return parse_gtfs_zip(static_zip)
