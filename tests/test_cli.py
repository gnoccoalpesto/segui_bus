"""CLI tests: argument parsing, exit codes and dispatch, with live data faked.

No network: ``fetch_live`` is replaced by a snapshot built from the sample feed.
"""

import pytest

import segui_bus.__main__ as cli
from segui_bus.clock import absolute_epoch, parse_service_date
from segui_bus.domain import (
    GeoPoint,
    TripRef,
    TripUpdateSnapshot,
    Vehicle,
    VehiclePosition,
    VehicleSnapshot,
)
from segui_bus.live import LiveData

DATE = "20261028"
NOW = absolute_epoch(parse_service_date(DATE), "19:40:00")
S1 = GeoPoint(45.052069, 9.705346)


def _vehicle(vehicle_id: str, trip_id: str, sequence: int) -> VehiclePosition:
    return VehiclePosition(
        vehicle=Vehicle(vehicle_id=vehicle_id, label=vehicle_id),
        timestamp=NOW,
        trip=TripRef(trip_id=trip_id, route_id="PC15", start_date=DATE, start_time="19:32:00"),
        position=S1,
        current_stop_sequence=sequence,
        current_status="IN_TRANSIT_TO",
    )


@pytest.fixture
def live(static) -> LiveData:
    vehicles = VehicleSnapshot(
        feed_timestamp=NOW,
        received_at=NOW,
        vehicles=(_vehicle("5284", "PC15-1", 1), _vehicle("6000", "PC15-2", 1)),
    )
    updates = TripUpdateSnapshot(feed_timestamp=NOW, received_at=NOW, trip_updates=())
    return LiveData(feed=static, vehicles=vehicles, updates=updates, now=NOW)


@pytest.fixture(autouse=True)
def fake_live(monkeypatch, live):
    monkeypatch.setattr(cli, "load_settings", lambda: None)
    monkeypatch.setattr(cli, "fetch_live", lambda settings: live)


def test_line_mode(capsys):
    assert cli.main(["line", "15"]) == 0

    out = capsys.readouterr().out
    assert "Line 15 (route PC15)" in out
    assert "5284" in out and "6000" in out


def test_unknown_line_exits_with_two(capsys):
    assert cli.main(["line", "99"]) == 2

    assert "unknown line: 99" in capsys.readouterr().out


def test_stop_mode_resolves_a_name(capsys):
    assert cli.main(["stop", "15", "marconi"]) == 0

    out = capsys.readouterr().out
    assert "stop S1" in out
    assert "strategy: fence" in out


def test_stop_mode_rejects_a_stop_the_line_does_not_serve(capsys):
    assert cli.main(["stop", "15", "crocetta"]) == 2

    assert "serves no stop" in capsys.readouterr().out


def test_stop_mode_lists_ambiguous_matches(capsys):
    # Both sample stops of line 15 contain an "s" ("Stazione", "corsia").
    assert cli.main(["stop", "15", "s"]) == 0

    out = capsys.readouterr().out
    assert "2 stops match" in out
    assert "using S3" in out


def test_vehicle_mode(capsys):
    assert cli.main(["vehicle", "5284"]) == 0

    out = capsys.readouterr().out
    assert "Vehicle 5284" in out
    assert "scheduled" in out


def test_vehicle_mode_with_a_stop(capsys):
    assert cli.main(["vehicle", "5284", "--stop", "marconi"]) == 0

    out = capsys.readouterr().out
    assert "Vehicle 5284" in out
    assert "S1" in out


def test_vehicle_mode_rejects_an_unknown_stop(capsys):
    assert cli.main(["vehicle", "5284", "--stop", "crocetta"]) == 2

    assert "serves no stop" in capsys.readouterr().out


def test_unknown_vehicle_exits_with_two(capsys):
    assert cli.main(["vehicle", "nope"]) == 2

    assert "not reporting" in capsys.readouterr().out


def test_radius_is_passed_through(capsys):
    assert cli.main(["stop", "15", "marconi", "--radius", "5"]) == 0

    # The header does not repeat the radius, but the run must still succeed.
    assert "Line 15" in capsys.readouterr().out
