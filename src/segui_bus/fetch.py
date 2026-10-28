"""One-shot feed fetchers.

The only place that turns Settings into realtime snapshots. The long-running
service will later swap HTTP for a WebSocket, but it will reuse these parsers
and the same domain objects.
"""

from __future__ import annotations

import httpx

from .config import Settings
from .domain import TripUpdateSnapshot, VehicleSnapshot
from .gtfsrt import parse_trip_updates, parse_vehicle_positions


def _get(settings: Settings, url: str) -> bytes:
    """One GET with the identification headers and the configured timeout."""
    with httpx.Client(headers=settings.headers(), timeout=settings.fetch_timeout_s) as http:
        response = http.get(url)
        response.raise_for_status()
        return response.content


def fetch_vehicle_positions(settings: Settings) -> VehicleSnapshot:
    """Where every bus is right now (FULL_DATASET snapshot)."""
    return parse_vehicle_positions(_get(settings, settings.feed_url("vehicle-positions")))


def fetch_trip_updates(settings: Settings) -> TripUpdateSnapshot:
    """Latest expected arrivals per trip (FULL_DATASET snapshot)."""
    return parse_trip_updates(_get(settings, settings.feed_url("trip-updates")))


def fetch_all(settings: Settings) -> tuple[VehicleSnapshot, TripUpdateSnapshot]:
    """Both realtime feeds. Sequential on purpose: concurrency 1 (research §2.3)."""
    return fetch_vehicle_positions(settings), fetch_trip_updates(settings)
