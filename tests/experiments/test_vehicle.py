"""Experiment: one bus, its itinerary and (optionally) its distance to a stop.

VEHICLE = None means "the first bus currently running LINE", which keeps the
scenario runnable without knowing today's matricole in advance.
"""

from __future__ import annotations

import pytest
from support import describe, live_only, stop_for, vehicle_for

from segui_bus.config import load_settings
from segui_bus.live import fetch_live
from segui_bus.report import format_table, vehicle_report

LINE = "15"
VEHICLE: str | None = None
STOP = "marconi"
RADIUS_M = 150.0


@pytest.fixture(scope="class")
def live():
    return fetch_live(load_settings())


@live_only
class VehicleExperimentTest:
    def test_itinerary_and_progress(self, live):
        vehicle_id = vehicle_for(live, LINE, VEHICLE)
        assert vehicle_id is not None, f"no vehicle is running line {LINE}"

        report = vehicle_report(live.feed, live.vehicles, live.updates, vehicle_id, now=live.now)
        assert report is not None, f"vehicle {vehicle_id} disappeared between fetches"
        print(format_table(report))

        assert report.candidate is not None
        assert report.itinerary, "the trip is missing from the static feed"

    def test_distance_to_a_stop(self, live):
        vehicle_id = vehicle_for(live, LINE, VEHICLE)
        stop = stop_for(live.feed, LINE, STOP)
        assert vehicle_id is not None and stop is not None

        report = vehicle_report(
            live.feed,
            live.vehicles,
            live.updates,
            vehicle_id,
            stop=stop,
            radius_m=RADIUS_M,
            now=live.now,
        )
        assert report is not None and report.candidate is not None

        print(format_table(report))
        print(describe(report.candidate))
