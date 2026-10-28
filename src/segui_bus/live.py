"""One-shot live data: the wiring the CLI and the experiment tests share.

Nothing here decides what to show; it only loads the static feed once and takes
one snapshot of each realtime feed. The long-running service will replace this
with a subscription loop and reuse the same report builders.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .config import Settings
from .domain import StaticFeed, TripUpdateSnapshot, VehicleSnapshot
from .fetch import fetch_all
from .static_gtfs import load_static


@dataclass(frozen=True, slots=True)
class LiveData:
    """Everything one experiment needs, all from the same moment."""

    feed: StaticFeed
    vehicles: VehicleSnapshot
    updates: TripUpdateSnapshot
    now: int


def fetch_live(settings: Settings, *, now: float | None = None) -> LiveData:
    """Load the static feed (cached) and fetch both realtime feeds once."""
    feed = load_static(settings)
    vehicles, updates = fetch_all(settings)
    return LiveData(
        feed=feed,
        vehicles=vehicles,
        updates=updates,
        now=int(time.time()) if now is None else int(now),
    )
