"""Watch engine tests, with fake snapshots and the synthetic GTFS feed.

Service day: 2026-10-28, 19:40 in Europe/Rome. The sample line 15 has
  PC15-1: S1 (19:32) -> S3 (19:40)
  PC15-2: S3 (19:32) -> S1 (19:45)
  PC15-3: S1 (21:00) -> S3 (21:05) -> S1 (21:12)   # loop
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
from segui_bus.geo import distance_m
from segui_bus.live import LiveData
from segui_bus.service.watch import PREAVVISO, REAL, WatchConfig, WatchEngine

DATE = "20261028"
NOW = absolute_epoch(parse_service_date(DATE), "19:40:00")
S1 = GeoPoint(45.052069, 9.705346)
S3 = GeoPoint(45.060000, 9.700000)
FAR = GeoPoint(45.100000, 9.800000)
NEAR_S1 = GeoPoint(45.053000, 9.705346)
OUTSIDE = GeoPoint(45.055000, 9.705346)
FURTHER = GeoPoint(45.058000, 9.705346)


def _vehicle(
    vehicle_id: str,
    trip_id: str,
    sequence: int,
    *,
    status: str = "IN_TRANSIT_TO",
    position: GeoPoint | None = NEAR_S1,
    stop_id: str | None = None,
    route_id: str = "PC15",
    start_date: str = DATE,
) -> VehiclePosition:
    return VehiclePosition(
        vehicle=Vehicle(vehicle_id=vehicle_id, label=vehicle_id),
        timestamp=NOW,
        trip=TripRef(
            trip_id=trip_id,
            route_id=route_id,
            start_date=start_date,
            start_time="19:32:00",
        ),
        position=position,
        stop_id=stop_id,
        current_stop_sequence=sequence,
        current_status=status,
    )


def _vehicles(*vehicles: VehiclePosition, age: int = 0) -> VehicleSnapshot:
    return VehicleSnapshot(
        feed_timestamp=NOW - age,
        received_at=NOW - age,
        vehicles=vehicles,
    )


def _update(
    trip_id: str,
    *,
    stop_sequence: int,
    delay_s: int,
    vehicle_id: str = "5284",
    start_date: str = DATE,
) -> TripUpdate:
    return TripUpdate(
        trip=TripRef(trip_id=trip_id, route_id="PC15", start_date=start_date),
        vehicle=Vehicle(vehicle_id=vehicle_id, label=vehicle_id),
        stop_time_updates=(
            StopTimeUpdate(
                stop_id="S1",
                stop_sequence=stop_sequence,
                arrival_delay_s=delay_s,
            ),
        ),
    )


def _updates(*updates: TripUpdate, age: int = 0) -> TripUpdateSnapshot:
    return TripUpdateSnapshot(
        feed_timestamp=NOW - age,
        received_at=NOW - age,
        trip_updates=updates,
    )


def _live(
    static,
    vehicles: VehicleSnapshot,
    updates: TripUpdateSnapshot | None = None,
) -> LiveData:
    return LiveData(
        feed=static,
        vehicles=vehicles,
        updates=updates if updates is not None else _updates(),
        now=NOW,
    )


def _watch(static, **overrides) -> WatchConfig:
    values = {
        "line": "15",
        "stop": static.stops["S1"],
        "radius_m": 150.0,
        "stops_n": 2,
        "lead_s": 300,
        "earliest": "00:00:00",
        "on_time_tolerance_s": 240,
        "assumed_speed_kmh": 20.0,
        "route_detour_factor": 1.3,
        "position_mismatch_m": 2000.0,
        "strategy": "nearest",
        "stale_after_s": 120,
    }
    values.update(overrides)
    return WatchConfig(**values)


# --- trigger arms ----------------------------------------------------------


def test_fence_arm_emits_a_real_alarm(static):
    engine = WatchEngine(_watch(static))
    live = _live(static, _vehicles(_vehicle("5284", "PC15-1", 1)))

    events = engine.tick(live)

    assert len(events) == 1
    event = events[0]
    assert event.kind == REAL
    assert event.vehicle_id == "5284"
    assert "fence" in event.arms
    assert event.stop_name == static.stops["S1"].name
    assert event.stops_to_target == 1
    assert event.snapshot_unix == NOW
    assert not event.stale
    assert not event.malformed
    assert event.on_time_tolerance_s == 240
    assert engine.alerted_trip == (DATE, "PC15-1")


def test_stops_arm_fires_outside_the_fence(static):
    engine = WatchEngine(_watch(static))
    live = _live(static, _vehicles(_vehicle("5284", "PC15-1", 1, position=OUTSIDE)))

    events = engine.tick(live)

    assert len(events) == 1
    assert events[0].kind == REAL
    assert "stops" in events[0].arms
    assert "fence" not in events[0].arms
    assert events[0].distance_m is not None
    assert events[0].distance_m > 150.0


def test_on_time_eta_is_a_real_alarm(static):
    engine = WatchEngine(_watch(static, stops_n=0, lead_s=400))
    vehicles = _vehicles(_vehicle("6000", "PC15-2", 1, position=OUTSIDE))
    updates = _updates(_update("PC15-2", stop_sequence=2, delay_s=0, vehicle_id="6000"))

    events = engine.tick(_live(static, vehicles, updates))

    assert len(events) == 1
    assert events[0].kind == REAL
    assert events[0].arms == ("eta",)
    assert events[0].vehicle_id == "6000"
    assert events[0].eta_source == "realtime"


def test_schedule_only_eta_is_a_preavviso(static):
    engine = WatchEngine(_watch(static, stops_n=0, lead_s=400))
    live = _live(static, _vehicles(_vehicle("6000", "PC15-2", 1, position=OUTSIDE)))

    events = engine.tick(live)

    assert len(events) == 1
    assert events[0].kind == PREAVVISO
    assert events[0].arms == ("eta",)
    assert events[0].eta_source == "schedule"
    assert engine.alerted_trip is None
    assert (DATE, "PC15-2") in engine.preavviso_sent


def test_late_realtime_eta_is_a_preavviso(static):
    engine = WatchEngine(_watch(static, stops_n=0, lead_s=1000))
    vehicles = _vehicles(_vehicle("6000", "PC15-2", 1, position=OUTSIDE))
    updates = _updates(_update("PC15-2", stop_sequence=2, delay_s=600, vehicle_id="6000"))

    events = engine.tick(_live(static, vehicles, updates))

    assert len(events) == 1
    assert events[0].kind == PREAVVISO
    assert events[0].delay_s == 600


def test_real_alarm_precedes_preavviso_on_the_same_tick(static):
    engine = WatchEngine(_watch(static, stops_n=0, lead_s=400))
    vehicles = _vehicles(
        _vehicle("5284", "PC15-1", 1),
        _vehicle("6000", "PC15-2", 1, position=FAR),
    )

    events = engine.tick(_live(static, vehicles))

    assert len(events) == 1
    assert events[0].kind == REAL
    assert events[0].vehicle_id == "5284"
    assert not engine.preavviso_sent


# --- selection -------------------------------------------------------------


def test_selection_uses_the_qualifying_subset(static):
    engine = WatchEngine(_watch(static, stops_n=1, lead_s=0))
    vehicles = _vehicles(
        _vehicle("6000", "PC15-2", 1, position=OUTSIDE),
        _vehicle("5284", "PC15-1", 1, position=FURTHER),
    )

    events = engine.tick(_live(static, vehicles))

    assert len(events) == 1
    assert events[0].vehicle_id == "5284"
    assert events[0].trip_id == "PC15-1"


def test_schedule_strategy_picks_the_earliest_qualifier(static):
    engine = WatchEngine(_watch(static, stops_n=2, strategy="schedule"))
    vehicles = _vehicles(
        _vehicle("5284", "PC15-1", 1, position=OUTSIDE),
        _vehicle("6000", "PC15-2", 1, position=FURTHER),
    )

    events = engine.tick(_live(static, vehicles))

    assert len(events) == 1
    assert events[0].vehicle_id == "5284"


# --- earliest --------------------------------------------------------------


def test_earliest_ignores_a_bus_that_arrives_too_early(static):
    engine = WatchEngine(_watch(static, earliest="19:36:00"))
    live = _live(static, _vehicles(_vehicle("5284", "PC15-1", 1)))

    assert engine.tick(live) == ()


def test_earliest_allows_a_bus_at_or_after_the_bound(static):
    engine = WatchEngine(_watch(static, earliest="19:30:00"))
    live = _live(static, _vehicles(_vehicle("5284", "PC15-1", 1)))

    assert len(engine.tick(live)) == 1


def test_malformed_ride_uses_now_for_earliest(static):
    engine = WatchEngine(_watch(static, earliest="19:36:00"))
    live = _live(static, _vehicles(_vehicle("5284", "MISSING", 1)))

    events = engine.tick(live)

    assert len(events) == 1
    assert events[0].malformed


# --- latch and release -----------------------------------------------------


def test_latch_blocks_a_repeat_of_the_same_trip(static):
    engine = WatchEngine(_watch(static))
    live = _live(static, _vehicles(_vehicle("5284", "PC15-1", 1)))

    assert len(engine.tick(live)) == 1
    assert engine.tick(live) == ()
    assert engine.alerted_trip == (DATE, "PC15-1")


def test_latch_blocks_another_qualifying_trip(static):
    engine = WatchEngine(_watch(static))
    first = _live(static, _vehicles(_vehicle("5284", "PC15-1", 1)))
    both = _live(
        static,
        _vehicles(
            _vehicle("5284", "PC15-1", 1),
            _vehicle("6000", "PC15-2", 1),
        ),
    )

    assert len(engine.tick(first)) == 1
    assert engine.tick(both) == ()


def test_latch_releases_when_the_alerted_vehicle_disappears(static):
    engine = WatchEngine(_watch(static))
    first = _live(static, _vehicles(_vehicle("5284", "PC15-1", 1)))
    second = _live(static, _vehicles(_vehicle("6000", "PC15-2", 1)))

    assert len(engine.tick(first)) == 1
    events = engine.tick(second)

    assert len(events) == 1
    assert events[0].vehicle_id == "6000"
    assert engine.alerted_trip == (DATE, "PC15-2")


def test_latch_releases_when_the_trip_key_changes(static):
    engine = WatchEngine(_watch(static, stops_n=2))
    first = _live(static, _vehicles(_vehicle("5284", "PC15-1", 1)))
    second = _live(
        static,
        _vehicles(_vehicle("5284", "PC15-2", 1, position=OUTSIDE)),
    )

    assert len(engine.tick(first)) == 1
    events = engine.tick(second)

    assert len(events) == 1
    assert events[0].trip_id == "PC15-2"
    assert engine.alerted_trip == (DATE, "PC15-2")


def test_leaving_the_fence_does_not_release_the_latch(static):
    engine = WatchEngine(_watch(static))
    first = _live(static, _vehicles(_vehicle("5284", "PC15-1", 1)))
    second = _live(static, _vehicles(_vehicle("5284", "PC15-1", 1, position=FAR)))

    assert len(engine.tick(first)) == 1
    assert engine.tick(second) == ()
    assert engine.alerted_trip == (DATE, "PC15-1")


def test_passing_the_stop_releases_the_latch_and_another_bus_fires(static):
    engine = WatchEngine(_watch(static))
    first = _live(static, _vehicles(_vehicle("5284", "PC15-1", 1)))
    second = _live(
        static,
        _vehicles(
            _vehicle("5284", "PC15-1", 2, status="STOPPED_AT", position=S3),
            _vehicle("7000", "PC15-3", 2, position=OUTSIDE),
        ),
    )

    assert len(engine.tick(first)) == 1
    events = engine.tick(second)

    assert len(events) == 1
    assert events[0].vehicle_id == "7000"
    assert engine.alerted_trip == (DATE, "PC15-3")


def test_malformed_latch_ignores_lost_stop_progress(static):
    engine = WatchEngine(_watch(static))
    wrong_update = _updates(_update("PC15-2", stop_sequence=2, delay_s=0))
    first = _live(static, _vehicles(_vehicle("5284", "PC15-1", 1)), wrong_update)
    second = _live(
        static,
        _vehicles(
            _vehicle("5284", "PC15-1", 2, status="STOPPED_AT", position=S3),
            _vehicle("7000", "PC15-3", 2, position=OUTSIDE),
        ),
        wrong_update,
    )

    assert len(engine.tick(first)) == 1
    assert engine.tick(second) == ()
    assert engine.alerted_trip == (DATE, "PC15-1")


def test_reset_clears_the_latch_and_sent_sets(static):
    engine = WatchEngine(_watch(static))
    live = _live(static, _vehicles(_vehicle("5284", "PC15-1", 1)))

    assert len(engine.tick(live)) == 1
    engine.reset()

    assert engine.alerted_trip is None
    assert engine.alarm_sent == set()
    assert engine.preavviso_sent == set()
    assert len(engine.tick(live)) == 1


# --- preavviso ordering ----------------------------------------------------


def test_preavviso_is_sent_once_then_the_real_alarm_still_fires(static):
    engine = WatchEngine(_watch(static, stops_n=0, lead_s=400))
    vehicles = _vehicles(_vehicle("6000", "PC15-2", 1, position=OUTSIDE))

    first = engine.tick(_live(static, vehicles))
    second = engine.tick(_live(static, vehicles))
    updates = _updates(_update("PC15-2", stop_sequence=2, delay_s=0, vehicle_id="6000"))
    third = engine.tick(_live(static, vehicles, updates))

    assert first[0].kind == PREAVVISO
    assert second == ()
    assert third[0].kind == REAL
    assert engine.alerted_trip == (DATE, "PC15-2")


def test_no_preavviso_after_the_real_alarm(static):
    engine = WatchEngine(_watch(static, stops_n=0, lead_s=400))
    vehicles = _vehicles(_vehicle("6000", "PC15-2", 1, position=OUTSIDE))
    on_time = _updates(_update("PC15-2", stop_sequence=2, delay_s=0, vehicle_id="6000"))
    late = _updates(_update("PC15-2", stop_sequence=2, delay_s=600, vehicle_id="6000"))

    assert engine.tick(_live(static, vehicles, on_time))[0].kind == REAL
    assert engine.tick(_live(static, vehicles, late)) == ()


# --- malformed rides -------------------------------------------------------


def test_unknown_trip_is_malformed_and_uses_the_fence_only(static):
    engine = WatchEngine(_watch(static))
    live = _live(static, _vehicles(_vehicle("5284", "MISSING", 1)))

    events = engine.tick(live)

    assert len(events) == 1
    assert events[0].malformed
    assert events[0].arms == ("fence",)
    assert events[0].stops_to_target is None
    assert events[0].eta_source is None


def test_unknown_sequence_is_malformed(static):
    engine = WatchEngine(_watch(static))
    live = _live(static, _vehicles(_vehicle("5284", "PC15-1", 99)))

    assert engine.tick(live)[0].malformed


def test_conflicting_vehicle_update_is_malformed(static):
    engine = WatchEngine(_watch(static))
    vehicles = _vehicles(_vehicle("5284", "PC15-1", 1))
    updates = _updates(_update("PC15-2", stop_sequence=2, delay_s=0))

    assert engine.tick(_live(static, vehicles, updates))[0].malformed


def test_conflicting_stop_id_is_malformed(static):
    engine = WatchEngine(_watch(static))
    live = _live(static, _vehicles(_vehicle("5284", "PC15-1", 1, stop_id="S3")))

    assert engine.tick(live)[0].malformed


def test_position_mismatch_is_malformed(static):
    engine = WatchEngine(_watch(static, position_mismatch_m=1.0))
    live = _live(static, _vehicles(_vehicle("5284", "PC15-1", 1)))

    assert engine.tick(live)[0].malformed


def test_malformed_ride_disables_the_stops_arm(static):
    engine = WatchEngine(_watch(static, stops_n=2, lead_s=2000))
    vehicles = _vehicles(_vehicle("5284", "PC15-1", 1, position=FAR))
    updates = _updates(_update("PC15-2", stop_sequence=2, delay_s=0))

    assert engine.tick(_live(static, vehicles, updates)) == ()


def test_malformed_ride_never_sends_a_preavviso(static):
    engine = WatchEngine(_watch(static, stops_n=0, lead_s=2000))
    vehicles = _vehicles(_vehicle("5284", "PC15-1", 1, position=FAR))
    updates = _updates(_update("PC15-2", stop_sequence=2, delay_s=0))

    assert engine.tick(_live(static, vehicles, updates)) == ()


# --- stale feeds -----------------------------------------------------------


def test_stale_fence_alarm_is_not_suppressed(static):
    engine = WatchEngine(_watch(static))
    live = _live(static, _vehicles(_vehicle("5284", "PC15-1", 1), age=500))

    events = engine.tick(live)

    assert len(events) == 1
    assert events[0].kind == REAL
    assert events[0].stale
    assert events[0].stale_age_s == 500


def test_stale_eta_takes_the_largest_projection(static):
    watch = _watch(
        static,
        stops_n=0,
        radius_m=1.0,
        lead_s=2200,
        position_mismatch_m=20000.0,
    )
    engine = WatchEngine(watch)
    live = _live(
        static,
        _vehicles(_vehicle("6000", "PC15-2", 1, position=FAR), age=500),
    )
    expected = watch.route_detour_factor * distance_m(FAR, static.stops["S1"].point) / (
        watch.assumed_speed_kmh / 3.6
    )

    events = engine.tick(live)

    assert len(events) == 1
    assert events[0].kind == PREAVVISO
    assert events[0].seconds_from_now == pytest.approx(round(expected))


def test_stale_on_time_eta_stays_a_real_alarm(static):
    engine = WatchEngine(
        _watch(
            static,
            stops_n=0,
            radius_m=1.0,
            lead_s=2200,
            position_mismatch_m=20000.0,
        )
    )
    vehicles = _vehicles(_vehicle("6000", "PC15-2", 1, position=FAR), age=500)
    updates = _updates(
        _update("PC15-2", stop_sequence=2, delay_s=0, vehicle_id="6000"),
        age=500,
    )

    events = engine.tick(_live(static, vehicles, updates))

    assert len(events) == 1
    assert events[0].kind == REAL
    assert events[0].stale


# --- event contract --------------------------------------------------------


def test_event_carries_the_notifier_fields(static):
    engine = WatchEngine(_watch(static))
    live = _live(static, _vehicles(_vehicle("5284", "PC15-1", 1)))

    event = engine.tick(live)[0]

    assert event.kind == REAL
    assert event.line == "15"
    assert event.headsign == "Ferrovia"
    assert event.vehicle_id == "5284"
    assert event.trip_id == "PC15-1"
    assert event.service_date == DATE
    assert event.route_id == "PC15"
    assert event.direction_id == 0
    assert event.distance_m == pytest.approx(103.0, abs=5.0)
    assert event.arrival_unix is not None
    assert event.seconds_from_now is not None
    assert event.on_time_tolerance_s == 240
    assert event.snapshot_unix == NOW
    assert event.stop_name
