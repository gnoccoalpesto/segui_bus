"""Parser tests.

Messages are built in code, so these tests are deterministic and need no
network and no recorded fixture. One shared helper builds a plausible feed.

The only exception is MultiBusTest at the bottom: a live, five-minute
observation that is skipped unless SEGUI_BUS_LIVE=1.
"""

import os
import time

import pytest
from google.transit import gtfs_realtime_pb2 as pb

from segui_bus.config import load_settings
from segui_bus.fetch import fetch_vehicle_positions
from segui_bus.gtfsrt import parse_trip_updates, parse_vehicle_positions

# --- builders --------------------------------------------------------------


def _feed(timestamp: int = 1_792_500_000) -> pb.FeedMessage:
    feed = pb.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = timestamp
    return feed


def _add_vehicle(feed: pb.FeedMessage, vehicle_id: str, *, timestamp: int) -> pb.FeedEntity:
    """Add a vehicle-position entity shaped like the real seta-pc feed."""
    entity = feed.entity.add()
    entity.id = vehicle_id

    v = entity.vehicle
    v.trip.trip_id = f"PC15-{vehicle_id}"
    v.trip.route_id = "PC15"
    v.trip.direction_id = 1
    v.trip.start_time = "19:32:00"
    v.trip.start_date = "20261020"
    v.vehicle.id = vehicle_id
    v.vehicle.label = vehicle_id
    v.position.latitude = 45.0521
    v.position.longitude = 9.6930
    v.position.bearing = 261.6
    v.stop_id = "PCU1005A"
    v.current_stop_sequence = 4
    v.current_status = pb.VehiclePosition.IN_TRANSIT_TO
    v.timestamp = timestamp
    return entity


def _add_trip_update(feed: pb.FeedMessage, trip_id: str, *, vehicle_id: str) -> pb.FeedEntity:
    entity = feed.entity.add()
    entity.id = trip_id

    tu = entity.trip_update
    tu.trip.trip_id = trip_id
    tu.trip.route_id = "PC15"
    tu.vehicle.id = vehicle_id
    tu.vehicle.label = vehicle_id

    first = tu.stop_time_update.add()
    first.stop_sequence = 1
    first.stop_id = "PCU2005B"
    first.arrival.delay = 120
    first.arrival.time = 1_792_505_040

    second = tu.stop_time_update.add()
    second.stop_sequence = 2
    second.stop_id = "PCU2006B"
    second.arrival.delay = 120
    second.arrival.time = 1_792_505_087
    return entity


# --- vehicle positions -----------------------------------------------------


def test_parses_one_vehicle_position():
    feed = _feed(timestamp=1000)
    _add_vehicle(feed, "5284", timestamp=999)
    snapshot = parse_vehicle_positions(feed.SerializeToString(), received_at=1001.5)

    assert snapshot.feed_timestamp == 1000
    assert snapshot.received_at == 1001.5
    assert len(snapshot.vehicles) == 1

    vp = snapshot.vehicles[0]
    assert vp.vehicle.vehicle_id == "5284"
    assert vp.trip is not None
    assert vp.trip.route_id == "PC15"  # line number comes later, via routes.txt
    assert vp.trip.direction_id == 1
    assert vp.position is not None
    # protobuf stores lat/lon as float32, hence the tolerance.
    assert vp.position.lat == pytest.approx(45.0521)
    assert vp.position.lon == pytest.approx(9.6930)
    assert vp.current_status == "IN_TRANSIT_TO"
    assert vp.timestamp == 999


def test_duplicate_vehicle_id_keeps_the_freshest():
    feed = _feed()
    _add_vehicle(feed, "5284", timestamp=100)
    _add_vehicle(feed, "5284", timestamp=200)

    snapshot = parse_vehicle_positions(feed.SerializeToString())

    assert len(snapshot.vehicles) == 1
    assert snapshot.vehicles[0].timestamp == 200


def test_vehicles_are_sorted_by_id():
    feed = _feed()
    _add_vehicle(feed, "5315", timestamp=100)
    _add_vehicle(feed, "5284", timestamp=100)

    snapshot = parse_vehicle_positions(feed.SerializeToString())

    assert [v.vehicle.vehicle_id for v in snapshot.vehicles] == ["5284", "5315"]


def test_entity_without_a_vehicle_is_ignored():
    feed = _feed()
    feed.entity.add().id = "not-a-vehicle"

    snapshot = parse_vehicle_positions(feed.SerializeToString())

    assert snapshot.vehicles == ()


def test_empty_vehicle_feed_is_a_valid_state():
    snapshot = parse_vehicle_positions(_feed(timestamp=42).SerializeToString())

    assert snapshot.feed_timestamp == 42
    assert snapshot.vehicles == ()


# --- trip updates ----------------------------------------------------------


def test_parses_trip_update_with_stop_times():
    feed = _feed()
    _add_trip_update(feed, "PC15-5857182", vehicle_id="5284")
    snapshot = parse_trip_updates(feed.SerializeToString(), received_at=2000.0)

    assert len(snapshot.trip_updates) == 1
    tu = snapshot.trip_updates[0]

    assert tu.trip.route_id == "PC15"
    assert tu.vehicle is not None
    assert tu.vehicle.vehicle_id == "5284"
    assert [s.stop_id for s in tu.stop_time_updates] == ["PCU2005B", "PCU2006B"]
    assert tu.stop_time_updates[0].arrival_delay_s == 120
    assert tu.stop_time_updates[0].arrival_time == 1_792_505_040


def test_duplicate_trip_id_is_kept_once():
    feed = _feed()
    _add_trip_update(feed, "PC15-5857182", vehicle_id="5284")
    _add_trip_update(feed, "PC15-5857182", vehicle_id="5284")

    snapshot = parse_trip_updates(feed.SerializeToString())

    assert len(snapshot.trip_updates) == 1


# --- bad input -------------------------------------------------------------


def test_garbage_payload_raises_value_error():
    with pytest.raises(ValueError, match="not a GTFS-RT FeedMessage"):
        parse_vehicle_positions(b"\xff\xff\xff\xff")


# --- live observation ------------------------------------------------------

LIVE = os.getenv("SEGUI_BUS_LIVE") == "1"

# The experiment's target lives here, not in a test name.
ROUTE_ID = "PC15"
POLL_EVERY_S = 30  # one sample per vehicle-positions emission (research note §3.2)
WATCH_FOR_S = 5 * 60


@pytest.mark.skipif(not LIVE, reason="live test: set SEGUI_BUS_LIVE=1 to run it")
class MultiBusTest:
    """Watch the buses of one route for five minutes.

    Opt-in because it needs the network and takes five minutes. Set
    SEGUI_BUS_LIVE=1 in .env and run it from the test explorer.
    """

    def test_buses_of_the_route_move_over_time(self):
        settings = load_settings()
        deadline = time.monotonic() + WATCH_FOR_S
        seen: set[str] = set()

        while time.monotonic() < deadline:
            snapshot = fetch_vehicle_positions(settings)

            # One progress line per sample: proves the loop is alive even when
            # no bus of the route is currently in the feed.
            print(f"-- {time.strftime('%H:%M:%S')} --", flush=True)

            for vp in snapshot.vehicles:
                # A vehicle without a trip or a position cannot be attributed to a route.
                if vp.trip is None or vp.trip.route_id != ROUTE_ID or vp.position is None:
                    continue
                seen.add(vp.vehicle.vehicle_id)
                # Raw floats on purpose: no rounding, so movement is visible.
                # flush=True because pytest pipes stdout, which is block-buffered:
                # without it the output only appears when the process ends.
                print(
                    f"{vp.vehicle.vehicle_id}: ({vp.position.lat}, {vp.position.lon})",
                    flush=True,
                )

            # Sleep only what is left of the window, so it ends at five minutes.
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(POLL_EVERY_S, remaining))

        assert seen, f"no vehicle served {ROUTE_ID} during the observation window"
