"""Experiment: every bus of a line, measured against one stop.

Change LINE/STOP/RADIUS_M here to explore another corner of the map. The three
selection strategies are shown side by side because they can disagree.
"""

from __future__ import annotations

import pytest
from support import describe, live_only, stop_for

from segui_bus.config import load_settings
from segui_bus.live import fetch_live
from segui_bus.report import STRATEGIES, format_table, stop_report

LINE = "15"
STOP = "marconi"
RADIUS_M = 150.0


@pytest.fixture(scope="class")
def live():
    return fetch_live(load_settings())


@live_only
class StopExperimentTest:
    def test_distance_and_stop_count_of_every_bus(self, live):
        stop = stop_for(live.feed, LINE, STOP)
        assert stop is not None, f'line {LINE} serves no stop matching "{STOP}"'

        report = stop_report(
            live.feed,
            live.vehicles,
            live.updates,
            LINE,
            stop,
            radius_m=RADIUS_M,
            now=live.now,
        )
        print(format_table(report))

        assert report.stop.stop_id == stop.stop_id

    def test_three_strategies_on_the_same_fetch(self, live):
        stop = stop_for(live.feed, LINE, STOP)
        assert stop is not None, f'line {LINE} serves no stop matching "{STOP}"'

        for strategy in STRATEGIES:
            report = stop_report(
                live.feed,
                live.vehicles,
                live.updates,
                LINE,
                stop,
                radius_m=RADIUS_M,
                now=live.now,
                strategy=strategy,
            )
            chosen = describe(report.selected) if report.selected else "none"
            print(f"{strategy:>8}: {chosen}")
