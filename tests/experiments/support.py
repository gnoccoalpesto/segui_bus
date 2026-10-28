"""Shared helpers for the live experiments.

These tests are opt-in and are meant to be run one scenario at a time from the
editor's test explorer, so no selection is expressed on the command line: each
module keeps its target in constants at the top.
"""

from __future__ import annotations

import os

import pytest

from segui_bus.domain import StaticFeed, Stop
from segui_bus.live import LiveData
from segui_bus.lookup import resolve_stops, vehicles_for_line
from segui_bus.report import Candidate

LIVE = os.getenv("SEGUI_BUS_LIVE") == "1"
live_only = pytest.mark.skipif(
    not LIVE, reason="live experiment: set SEGUI_BUS_LIVE=1 in .env to run it"
)


def stop_for(feed: StaticFeed, line: str, query: str) -> Stop | None:
    """First stop matching a name/ID/coordinates on the line, or None."""
    matches = resolve_stops(feed, line, query)
    return matches[0] if matches else None


def vehicle_for(live: LiveData, line: str, preferred: str | None) -> str | None:
    """The requested vehicle, or the first one running the line."""
    if preferred:
        return preferred
    running = vehicles_for_line(live.feed, live.vehicles, line)
    return running[0].vehicle.vehicle_id if running else None


def describe(candidate: Candidate) -> str:
    """One-line summary used in experiment assertions and messages."""
    return (
        f"{candidate.vehicle_id} trip={candidate.trip_id} "
        f"line={candidate.line} direction={candidate.direction_id} "
        f"stops_to_target={candidate.stops_to_target} "
        f"distance_m={candidate.distance_m} in_fence={candidate.in_fence}"
    )
