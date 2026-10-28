"""Progress tests: the status off-by-one, loop lines and along-route distances.

The sample feed (tests/gtfs_sample.py) has:
  PC15-1: S1 (seq 1) -> S3 (seq 2)
  PC15-3: S1 (seq 1) -> S3 (seq 2) -> S1 (seq 3)   # a loop, same stop twice
"""

import pytest

from segui_bus.domain import GeoPoint, TripRef, Vehicle, VehiclePosition
from segui_bus.geo import distance_m
from segui_bus.progress import progress_for

S1 = GeoPoint(45.052069, 9.705346)
S3 = GeoPoint(45.060000, 9.700000)
S1_TO_S3_M = distance_m(S1, S3)


def _vehicle(
    trip_id: str,
    *,
    status: str = "IN_TRANSIT_TO",
    sequence: int | None = None,
    stop_id: str | None = None,
    position: GeoPoint | None = None,
) -> VehiclePosition:
    return VehiclePosition(
        vehicle=Vehicle(vehicle_id="5284", label="5284"),
        timestamp=1_000_000,
        trip=TripRef(trip_id=trip_id, route_id="PC15"),
        position=position,
        stop_id=stop_id,
        current_stop_sequence=sequence,
        current_status=status,
    )


def test_heading_to_a_stop_credits_only_the_previous_one(static):
    progress = progress_for(static, _vehicle("PC15-1", sequence=2))

    assert progress.next_index == 1
    assert progress.served_index == 0
    assert progress.next_stop_id == "S3"
    assert progress.served_stop_id == "S1"
    assert progress.stops_total == 2
    assert progress.stops_left == 1
    assert progress.distance_done_m == pytest.approx(0.0)
    assert progress.distance_left_m == pytest.approx(S1_TO_S3_M)
    assert progress.fraction == pytest.approx(0.0)


def test_stopped_at_a_stop_counts_it_as_served(static):
    progress = progress_for(static, _vehicle("PC15-1", status="STOPPED_AT", sequence=2))

    assert progress.next_index == progress.served_index == 1
    assert progress.stops_left == 0
    assert progress.distance_done_m == pytest.approx(S1_TO_S3_M)
    assert progress.distance_left_m == pytest.approx(0.0)
    assert progress.fraction == pytest.approx(1.0)


def test_loop_trip_anchors_on_the_sequence_not_on_the_stop_id(static):
    # PC15-3 is S1 -> S3 -> S1. At sequence 3 the bus has served S3, not S1.
    progress = progress_for(static, _vehicle("PC15-3", sequence=3))

    assert progress.next_index == 2
    assert progress.served_index == 1
    assert progress.served_stop_id == "S3"
    assert progress.next_stop_id == "S1"
    assert progress.stops_left == 1
    assert progress.route_length_m == pytest.approx(2 * S1_TO_S3_M)
    assert progress.distance_done_m == pytest.approx(S1_TO_S3_M)
    assert progress.fraction == pytest.approx(0.5)


def test_before_the_first_stop_nothing_is_served(static):
    progress = progress_for(static, _vehicle("PC15-1", sequence=1))

    assert progress.next_index == 0
    assert progress.served_index == -1
    assert progress.served_stop_id is None
    assert progress.stops_left == 2
    assert progress.distance_done_m == pytest.approx(0.0)


def test_missing_sequence_falls_back_to_the_stop_id(static):
    progress = progress_for(static, _vehicle("PC15-3", stop_id="S1"))

    # Without a position the first occurrence wins.
    assert progress.next_index == 0
    assert progress.served_index == -1


def test_missing_sequence_falls_back_to_the_nearest_stop(static):
    # A point right next to S3, with no sequence and no stop_id at all.
    progress = progress_for(static, _vehicle("PC15-1", position=GeoPoint(45.0601, 9.7001)))

    assert progress.next_index == 1
    assert progress.next_stop_id == "S3"


def test_unknown_trip_has_no_progress(static):
    assert progress_for(static, _vehicle("PC99-1", sequence=1)) is None


def test_vehicle_without_a_trip_has_no_progress(static):
    vehicle = VehiclePosition(vehicle=Vehicle(vehicle_id="1", label="1"), timestamp=0)

    assert progress_for(static, vehicle) is None


def test_no_usable_anchor_still_reports_the_route_length(static):
    progress = progress_for(static, _vehicle("PC15-1"))

    assert progress.next_index is None
    assert progress.served_index is None
    assert progress.stops_left == 2
    assert progress.distance_done_m == pytest.approx(0.0)
    assert progress.distance_left_m == pytest.approx(S1_TO_S3_M)
    assert progress.to_first_stop_m is None
    assert progress.to_last_stop_m is None


def test_straight_line_distances_to_the_termini(static):
    position = GeoPoint(45.055, 9.702)

    progress = progress_for(static, _vehicle("PC15-1", sequence=1, position=position))

    assert progress.to_first_stop_m == pytest.approx(distance_m(position, S1))
    assert progress.to_last_stop_m == pytest.approx(distance_m(position, S3))


def test_geometry_is_cached_per_itinerary_shape(static):
    # Asking twice must not change the answer; the cache is keyed by the points.
    first = progress_for(static, _vehicle("PC15-1", sequence=2))
    second = progress_for(static, _vehicle("PC15-1", sequence=2))

    assert first == second
