"""Experiment: watch every bus of one line and its progress along the trip.

Target of the experiment, not of a command-line filter.
"""

from __future__ import annotations

import pytest
from support import live_only

from segui_bus.config import load_settings
from segui_bus.live import fetch_live
from segui_bus.report import format_table, line_report

LINE = "15"
STRATEGY = "nearest"


@pytest.fixture(scope="class")
def live():
    """One real fetch for the whole class (static feed is cached on disk)."""
    return fetch_live(load_settings())


@live_only
class LineExperimentTest:
    def test_every_bus_of_the_line_and_its_progress(self, live):
        report = line_report(
            live.feed,
            live.vehicles,
            live.updates,
            LINE,
            now=live.now,
            strategy=STRATEGY,
        )
        print(format_table(report))

        assert report.route_id is not None, f"line {LINE} is not in gtfs.zip"

    def test_candidates_keep_their_progress_objects(self, live):
        report = line_report(
            live.feed, live.vehicles, live.updates, LINE, now=live.now, strategy=STRATEGY
        )

        for candidate in report.candidates:
            # Every vehicle of the line is executing a trip in the static feed.
            assert candidate.progress is not None, candidate.vehicle_id
            assert candidate.progress.stops_total > 0
