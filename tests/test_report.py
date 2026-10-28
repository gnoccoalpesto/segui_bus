"""Report tests: candidate fields, selection strategies and plain-text output.

Times: 2026-10-28, 19:40. The sample feed has
  PC15-1: S1 (19:32) -> S3 (19:40)
  PC15-2: S3 (19:32) -> S1 (19:45)
  PC15-3: S1 (08:00) -> S3 (08:05) -> S1 (08:12)   # loop
"""

import pytest

from segui_bus.clock import absolute_epoch, parse_service_date
from segui_bus.domain import (
    GeoPoint,
    StopTimeUpdate,
    TripRef,
    TripUpdate,
    TripUpdateSnapshot,
    Vehicle,
    VehiclePosition,
    VehicleSnapshot,
)
from segui_bus.report import (
    format_table,
    line_report,
    select_vehicle,
    stop_report,
    vehicle_report,
)

DATE = "20261028"
NOW = absolute_epoch(parse_service_date(DATE), "19:40:00")
S1 = GeoPoint(45.052069, 9.705346)
S3 = GeoPoint(45.060000, 9.700000)
FAR = GeoPoint(45.100000, 9.800000)
NEAR_S1 = GeoPoint(45.053000, 9.705346)  # roughly 100 m north of S1


def _vehicle(
    vehicle_id: str,
    trip_id: str,
    sequence: int,
    *,
    status: str = "IN_TRANSIT_TO",
    position: GeoPoint = FAR,
) -> VehiclePosition:
    return VehiclePosition(
        vehicle=Vehicle(vehicle_id=vehicle_id, label=vehicle_id),
        timestamp=NOW,
        trip=TripRef(trip_id=trip_id, route_id="PC15", start_date=DATE, start_time="19:32:00"),
        position=position,
        current_stop_sequence=sequence,
        current_status=status,
    )


def _vehicles(*vehicles: VehiclePosition, age: int = 0) -> VehicleSnapshot:
    return VehicleSnapshot(
        feed_timestamp=NOW - age,
        received_at=NOW - age,
        vehicles=vehicles,
    )


def _updates(*updates: TripUpdate, age: int = 0) -> TripUpdateSnapshot:
    return TripUpdateSnapshot(
        feed_timestamp=NOW - age,
        received_at=NOW - age,
        trip_updates=updates,
    )


def _update(trip_id: str, *, stop_sequence: int, delay_s: int) -> TripUpdate:
    return TripUpdate(
        trip=TripRef(trip_id=trip_id, route_id="PC15", start_date=DATE),
        vehicle=Vehicle(vehicle_id="6000", label="6000"),
        stop_time_updates=(
            StopTimeUpdate(stop_id="S1", stop_sequence=stop_sequence, arrival_delay_s=delay_s),
        ),
    )


def _fleet() -> tuple[VehicleSnapshot, TripUpdateSnapshot]:
    """Three buses: heading to S1, standing at S1, and on the loop at S3."""
    vehicles = _vehicles(
        _vehicle("7000", "PC15-3", 2, position=S3),
        _vehicle("5284", "PC15-1", 1, position=NEAR_S1),
        _vehicle("6000", "PC15-2", 2, status="STOPPED_AT", position=S1),
    )
    updates = _updates(_update("PC15-2", stop_sequence=2, delay_s=120))
    return vehicles, updates


# --- line report -----------------------------------------------------------


def test_line_report_has_one_candidate_per_bus(static):
    vehicles, updates = _fleet()

    report = line_report(static, vehicles, updates, "15", now=NOW)

    assert report.route_id == "PC15"
    assert [candidate.vehicle_id for candidate in report.candidates] == ["5284", "6000", "7000"]
    assert all(candidate.line == "15" for candidate in report.candidates)
    assert report.feed.calendar_covered
    assert not report.feed.stale


def test_line_report_enriches_the_static_trip(static):
    vehicles, updates = _fleet()

    report = line_report(static, vehicles, updates, "15", now=NOW)
    by_id = {candidate.vehicle_id: candidate for candidate in report.candidates}

    assert by_id["6000"].direction_id == 1  # PC15-2
    assert by_id["6000"].headsign == "Marconi"
    assert by_id["6000"].service_date == DATE


def test_line_report_has_no_distance_so_nearest_selects_nothing(static):
    vehicles, updates = _fleet()

    report = line_report(static, vehicles, updates, "15", now=NOW, strategy="nearest")

    # Without a stop there is no distance: the table still shows every bus.
    assert report.selected is None
    assert len(report.candidates) == 3


# --- stop report -----------------------------------------------------------


def test_stop_report_marks_the_served_and_the_upcoming_visit(static):
    vehicles, updates = _fleet()

    report = stop_report(static, vehicles, updates, "15", static.stops["S1"], now=NOW)
    by_id = {candidate.vehicle_id: candidate for candidate in report.candidates}

    # 5284 is heading to S1: one stop to go, still at the schedule.
    assert by_id["5284"].serves_stop
    assert by_id["5284"].stops_to_target == 1
    assert by_id["5284"].target_index == 0
    assert by_id["5284"].eta_source == "schedule"
    assert by_id["5284"].delay_s is None

    # 6000 is standing at S1: zero stops to go, realtime delay.
    assert by_id["6000"].stops_to_target == 0
    assert by_id["6000"].target_index == 1
    assert by_id["6000"].eta_source == "realtime"
    assert by_id["6000"].delay_s == 120
    assert by_id["6000"].arrival_unix == absolute_epoch(parse_service_date(DATE), "19:45:00") + 120


def test_stop_report_uses_the_sequence_of_the_loop_visit(static):
    vehicles, updates = _fleet()

    report = stop_report(static, vehicles, updates, "15", static.stops["S1"], now=NOW)
    loop = next(candidate for candidate in report.candidates if candidate.vehicle_id == "7000")

    # PC15-3 has served S1 (index 0) and S3 (index 1); the next S1 is index 2.
    assert loop.progress_index == 0
    assert loop.stops_to_target == 2
    assert loop.target_index == 2


def test_stop_report_distances_and_fence(static):
    vehicles, updates = _fleet()

    report = stop_report(static, vehicles, updates, "15", static.stops["S1"], now=NOW)
    by_id = {candidate.vehicle_id: candidate for candidate in report.candidates}

    assert by_id["6000"].distance_m == pytest.approx(0.0, abs=1.0)
    assert by_id["6000"].in_fence
    assert by_id["5284"].in_fence
    assert not by_id["7000"].in_fence


def test_fence_strategy_picks_the_furthest_bus_inside(static):
    vehicles, updates = _fleet()

    report = stop_report(static, vehicles, updates, "15", static.stops["S1"], now=NOW)

    # Both 5284 and 6000 are inside the fence; 6000 has finished its trip.
    assert report.selected is not None
    assert report.selected.vehicle_id == "6000"


def test_schedule_strategy_picks_the_earliest_arrival(static):
    vehicles, updates = _fleet()

    report = stop_report(
        static, vehicles, updates, "15", static.stops["S1"], now=NOW, strategy="schedule"
    )

    assert report.selected is not None
    assert report.selected.vehicle_id == "5284"


def test_nearest_strategy_picks_the_smallest_distance(static):
    vehicles, updates = _fleet()

    report = stop_report(
        static, vehicles, updates, "15", static.stops["S1"], now=NOW, strategy="nearest"
    )

    assert report.selected is not None
    assert report.selected.vehicle_id == "6000"


def test_unknown_strategy_is_rejected(static):
    with pytest.raises(ValueError):
        select_vehicle((), "teleport")


def test_selecting_from_nothing_returns_none():
    assert select_vehicle((), "fence") is None
    assert select_vehicle((), "schedule") is None
    assert select_vehicle((), "nearest") is None


def test_stale_snapshots_are_flagged(static):
    vehicles = _vehicles(_vehicle("5284", "PC15-1", 1), age=500)
    updates = _updates(age=500)

    report = stop_report(static, vehicles, updates, "15", static.stops["S1"], now=NOW)

    assert report.feed.stale
    assert all(candidate.stale for candidate in report.candidates)


# --- vehicle report --------------------------------------------------------


def test_vehicle_report_marks_the_itinerary(static):
    vehicles, updates = _fleet()

    report = vehicle_report(static, vehicles, updates, "5284", now=NOW)

    assert report.line == "15"
    assert report.trip_id == "PC15-1"
    assert [row.stop_id for row in report.itinerary] == ["S1", "S3"]
    assert [row.next for row in report.itinerary] == [True, False]
    assert [row.served for row in report.itinerary] == [False, False]


def test_vehicle_report_marks_a_stopped_bus_as_served(static):
    vehicles, updates = _fleet()

    report = vehicle_report(static, vehicles, updates, "6000", now=NOW)

    last = report.itinerary[-1]
    assert last.stop_id == "S1"
    assert last.served and last.next
    assert last.delay_s == 120
    assert last.source == "realtime"


def test_vehicle_report_uses_the_loop_visit(static):
    vehicles, updates = _fleet()

    report = vehicle_report(static, vehicles, updates, "7000", now=NOW)

    assert [row.stop_id for row in report.itinerary] == ["S1", "S3", "S1"]
    assert [row.served for row in report.itinerary] == [True, False, False]
    assert [row.next for row in report.itinerary] == [False, True, False]


def test_vehicle_report_can_measure_a_stop(static):
    vehicles, updates = _fleet()

    report = vehicle_report(static, vehicles, updates, "5284", stop=static.stops["S1"], now=NOW)

    assert report.candidate.serves_stop
    assert report.candidate.distance_m is not None
    assert report.candidate.stops_to_target == 1


def test_vehicle_report_without_a_trip(static):
    vehicle = VehiclePosition(vehicle=Vehicle(vehicle_id="1", label="1"), timestamp=NOW)
    vehicles = _vehicles(vehicle)

    report = vehicle_report(static, vehicles, _updates(), "1", now=NOW)

    assert report.candidate is None
    assert report.itinerary == ()


def test_unknown_vehicle_has_no_report(static):
    vehicles, updates = _fleet()

    assert vehicle_report(static, vehicles, updates, "does-not-exist", now=NOW) is None


# --- formatting ------------------------------------------------------------


def test_stop_table_marks_the_selected_vehicle(static):
    vehicles, updates = _fleet()
    report = stop_report(static, vehicles, updates, "15", static.stops["S1"], now=NOW)

    table = format_table(report)

    assert "Line 15 (route PC15)" in table
    assert "stop S1 P.le Marconi" in table
    assert "strategy: fence" in table
    assert "5284" in table and "6000" in table and "7000" in table
    selected_rows = [line for line in table.splitlines() if line.lstrip().startswith("*")]
    assert len(selected_rows) == 1
    assert "6000" in selected_rows[0]


def test_line_table_without_buses_still_renders(static):
    report = line_report(static, _vehicles(), _updates(), "15", now=NOW)

    assert "(no vehicle of this line" in format_table(report)


def test_vehicle_table_shows_the_itinerary_marks(static):
    vehicles, updates = _fleet()
    report = vehicle_report(static, vehicles, updates, "7000", now=NOW)

    table = format_table(report)

    assert "Vehicle 7000" in table
    assert "scheduled" in table and "expected" in table
    assert "S1" in table and "S3" in table
    assert ">" in table  # the next row
    assert "*" in table  # a served row
