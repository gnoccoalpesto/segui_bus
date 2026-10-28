"""ETA, fence and stop-count tests.

Scheduled base used throughout: PC15-1 leaves S1 at 19:32 and S3 at 19:40 on
2026-10-28. The sample loop PC15-3 is S1 -> S3 -> S1, so sequence matching is
tested with a stop that appears twice.
"""

import pytest

from segui_bus.clock import absolute_epoch, parse_service_date
from segui_bus.domain import (
    GeoPoint,
    StopTimeUpdate,
    TripRef,
    TripUpdate,
    Vehicle,
    VehiclePosition,
)
from segui_bus.geo import distance_m
from segui_bus.progress import progress_for
from segui_bus.proximity import distance_to_stop, eta_to_stop, in_fence, stops_to_target

DATE = "20261028"
S3_AT_1940 = absolute_epoch(parse_service_date(DATE), "19:40:00")
NEAR_S1 = GeoPoint(45.0521, 9.7053)


def _vehicle(
    trip_id: str = "PC15-1",
    *,
    sequence: int | None = None,
    status: str | None = None,
    position: GeoPoint | None = NEAR_S1,
) -> VehiclePosition:
    return VehiclePosition(
        vehicle=Vehicle(vehicle_id="5284", label="5284"),
        timestamp=1_000_000,
        trip=TripRef(trip_id=trip_id, route_id="PC15", start_date=DATE, start_time="19:32:00"),
        position=position,
        current_stop_sequence=sequence,
        current_status=status,
    )


def _update(trip_id: str, *stop_updates: StopTimeUpdate) -> TripUpdate:
    return TripUpdate(
        trip=TripRef(trip_id=trip_id, route_id="PC15", start_date=DATE),
        vehicle=Vehicle(vehicle_id="5284", label="5284"),
        stop_time_updates=stop_updates,
    )


# --- eta -------------------------------------------------------------------


def test_realtime_arrival_time_is_used_as_is(static):
    update = _update(
        "PC15-1", StopTimeUpdate(stop_id="S3", stop_sequence=2, arrival_time=S3_AT_1940 + 180)
    )

    eta = eta_to_stop(static, "PC15-1", 2, service_date=DATE, update=update, now=S3_AT_1940)

    assert eta.source == "realtime"
    assert eta.expected_unix == S3_AT_1940 + 180
    assert eta.delay_s == 180  # derived: no delay field on the update
    assert eta.seconds_from_now == 180


def test_explicit_negative_delay_is_kept(static):
    update = _update("PC15-1", StopTimeUpdate(stop_id="S3", stop_sequence=2, arrival_delay_s=-90))

    eta = eta_to_stop(static, "PC15-1", 2, service_date=DATE, update=update, now=S3_AT_1940)

    assert eta.expected_unix == S3_AT_1940 - 90
    assert eta.delay_s == -90
    assert eta.seconds_from_now == -90


def test_arrival_time_wins_over_a_conflicting_delay(static):
    update = _update(
        "PC15-1",
        StopTimeUpdate(
            stop_id="S3", stop_sequence=2, arrival_time=S3_AT_1940 + 60, arrival_delay_s=300
        ),
    )

    eta = eta_to_stop(static, "PC15-1", 2, service_date=DATE, update=update, now=S3_AT_1940)

    assert eta.expected_unix == S3_AT_1940 + 60
    assert eta.delay_s == 300


def test_no_update_falls_back_to_the_schedule(static):
    eta = eta_to_stop(static, "PC15-1", 2, service_date=DATE, now=S3_AT_1940)

    assert eta.source == "schedule"
    assert eta.expected_unix == S3_AT_1940
    assert eta.delay_s is None
    assert eta.seconds_from_now == 0


def test_update_for_another_trip_is_ignored(static):
    update = _update("PC15-2", StopTimeUpdate(stop_id="S3", stop_sequence=2, arrival_delay_s=999))

    eta = eta_to_stop(static, "PC15-1", 2, service_date=DATE, update=update, now=S3_AT_1940)

    assert eta.source == "schedule"


def test_update_without_the_wanted_sequence_falls_back(static):
    update = _update("PC15-1", StopTimeUpdate(stop_id="S3", stop_sequence=1, arrival_delay_s=120))

    eta = eta_to_stop(static, "PC15-1", 2, service_date=DATE, update=update, now=S3_AT_1940)

    assert eta.source == "schedule"


def test_loop_stop_is_matched_by_sequence_not_by_stop_id(static):
    # Two updates for the same stop_id (S1), different visits. Asking for the
    # third stop must take the second update, not the first.
    update = _update(
        "PC15-3",
        StopTimeUpdate(stop_id="S1", stop_sequence=1, arrival_delay_s=60),
        StopTimeUpdate(stop_id="S1", stop_sequence=3, arrival_delay_s=240),
    )

    eta = eta_to_stop(static, "PC15-3", 3, service_date=DATE, update=update, now=0)

    assert eta.delay_s == 240


def test_unknown_sequence_has_no_eta(static):
    assert eta_to_stop(static, "PC15-1", 99, service_date=DATE) is None


# --- distance and fence ----------------------------------------------------


def test_distance_to_stop(static):
    vehicle = _vehicle()

    assert distance_to_stop(vehicle, static.stops["S1"]) == pytest.approx(0.0, abs=5.0)
    assert distance_to_stop(vehicle, static.stops["S3"]) > 500.0


def test_distance_without_a_position_is_unknown(static):
    assert distance_to_stop(_vehicle(position=None), static.stops["S1"]) is None


def test_fence_is_inclusive(static):
    stop = static.stops["S1"]
    nearby = GeoPoint(45.0600, 9.7000)
    radius = distance_m(nearby, stop.point)

    assert in_fence(_vehicle(position=nearby), stop, radius)
    assert not in_fence(_vehicle(position=nearby), stop, radius - 1.0)
    assert not in_fence(_vehicle(position=GeoPoint(45.0600, 9.7000)), stop, 50.0)


def test_fence_without_a_position_is_false(static):
    assert not in_fence(_vehicle(position=None), static.stops["S1"], 10_000.0)


# --- stops to target -------------------------------------------------------


def test_stops_to_target_normally_counts_from_the_last_served_stop(static):
    progress = progress_for(static, _vehicle(sequence=1))

    # Heading for S1 (index 0) with nothing served yet: S3 is 2 stops away.
    assert stops_to_target(static, progress, "S3") == 2


def test_stops_to_target_is_zero_while_standing_at_the_stop(static):
    progress = progress_for(static, _vehicle(sequence=2, status="STOPPED_AT"))

    assert stops_to_target(static, progress, "S3") == 0


def test_stops_to_target_follows_the_current_loop_visit(static):
    # On PC15-3 at sequence 2 the bus has left S1 (index 0) and is heading to
    # S3. The next S1 visit is index 2, two stops from the last served one.
    progress = progress_for(static, _vehicle("PC15-3", sequence=2))

    assert stops_to_target(static, progress, "S1") == 2


def test_stops_to_target_before_the_first_stop(static):
    progress = progress_for(static, _vehicle(sequence=1))

    assert stops_to_target(static, progress, "S1") == 1
    assert stops_to_target(static, progress, "S3") == 2


def test_stops_to_target_returns_none_when_not_served_any_more(static):
    progress = progress_for(static, _vehicle(sequence=2, status="STOPPED_AT"))

    assert stops_to_target(static, progress, "S1") is None
