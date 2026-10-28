"""Lookup tests: line joins, stop resolution and TripRef enrichment.

The feed comes from tests/conftest.py (tests/gtfs_sample.py): line 15 serves S1
and S3, line 1 serves S4 only. S2 exists but no line serves it, which is the
case that catches a lookup that forgets to filter by line.
"""

from segui_bus.domain import TripRef, Vehicle, VehiclePosition, VehicleSnapshot
from segui_bus.lookup import (
    enrich_lines,
    resolve_stops,
    stop_at,
    stop_ids_of_line,
    stops_of_line,
    vehicles_for_line,
)


def _snapshot(*trip_refs: TripRef | None) -> VehicleSnapshot:
    return VehicleSnapshot(
        feed_timestamp=1000,
        received_at=0.0,
        vehicles=tuple(
            VehiclePosition(
                vehicle=Vehicle(vehicle_id=str(index), label=str(index)),
                timestamp=1000,
                trip=trip,
            )
            for index, trip in enumerate(trip_refs)
        ),
    )


def test_vehicles_for_line_keeps_only_that_route(static):
    snapshot = _snapshot(
        TripRef(trip_id="PC15-1", route_id="PC15"),
        TripRef(trip_id="PC1-1", route_id="PC1"),
        TripRef(trip_id="PC15-9", route_id="PC15"),
        None,  # a vehicle with no trip cannot be attributed
    )

    found = vehicles_for_line(static, snapshot, "15")

    assert [vehicle.vehicle.vehicle_id for vehicle in found] == ["0", "2"]


def test_vehicles_for_line_accepts_the_human_number_not_the_route_id(static):
    snapshot = _snapshot(TripRef(trip_id="PC15-1", route_id="PC15"))

    assert vehicles_for_line(static, snapshot, "PC15") == ()
    assert len(vehicles_for_line(static, snapshot, " 15 ")) == 1


def test_unknown_line_yields_nothing(static):
    snapshot = _snapshot(TripRef(trip_id="PC15-1", route_id="PC15"))

    assert vehicles_for_line(static, snapshot, "99") == ()
    assert stop_ids_of_line(static, "99") == frozenset()


def test_stop_ids_of_line_unions_both_directions(static):
    assert stop_ids_of_line(static, "15") == frozenset({"S1", "S3"})
    assert stop_ids_of_line(static, "1") == frozenset({"S4"})


def test_stops_of_line_are_ordered_by_name(static):
    assert [stop.stop_id for stop in stops_of_line(static, "15")] == ["S3", "S1"]


def test_stop_at(static):
    assert stop_at(static, "S1").name.startswith("P.le Marconi")
    assert stop_at(static, "nope") is None


def test_resolve_stop_by_id(static):
    assert [stop.stop_id for stop in resolve_stops(static, "15", "S1")] == ["S1"]


def test_resolve_stop_by_name_is_case_insensitive(static):
    assert [stop.stop_id for stop in resolve_stops(static, "15", "marconi")] == ["S1"]


def test_resolve_stop_is_filtered_through_the_line(static):
    # The text matches a real stop, but line 15 never serves it.
    assert resolve_stops(static, "15", "crocetta") == []
    assert [stop.stop_id for stop in resolve_stops(static, "1", "crocetta")] == ["S4"]


def test_resolve_stop_by_coordinates_returns_nearest_first(static):
    # A point next to S1; S2 is closer but not served by line 15.
    found = resolve_stops(static, "15", "45.0521,9.7053")

    assert [stop.stop_id for stop in found] == ["S1", "S3"]


def test_resolve_stop_text_that_looks_like_coordinates_but_is_not(static):
    # "1,2" parses as a point, so it must not be treated as a name substring.
    found = resolve_stops(static, "15", "1,2")

    assert [stop.stop_id for stop in found] == ["S1", "S3"]


def test_enrich_lines_fills_line_and_missing_direction(static):
    trip = TripRef(trip_id="PC15-2", route_id="PC15")

    (enriched,) = enrich_lines(static, [trip])

    assert enriched.line == "15"
    assert enriched.direction_id == 1  # from trips.txt


def test_enrich_lines_keeps_an_explicit_direction(static):
    trip = TripRef(trip_id="PC15-2", route_id="PC15", direction_id=0, line="already")

    (enriched,) = enrich_lines(static, [trip])

    assert enriched.direction_id == 0
    assert enriched.line == "15"


def test_enrich_lines_leaves_unknown_trips_alone(static):
    trip = TripRef(trip_id="missing", route_id="PC99")

    (enriched,) = enrich_lines(static, [trip])

    assert enriched == trip
