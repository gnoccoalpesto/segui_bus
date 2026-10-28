"""Fetcher tests: respx fakes the transport, the parsers do the real work."""

from dataclasses import replace

import httpx
import pytest
import respx
from google.transit import gtfs_realtime_pb2 as pb

from segui_bus.config import load_settings
from segui_bus.fetch import fetch_all, fetch_trip_updates, fetch_vehicle_positions

VP_URL = "https://proxy.busone.app/seta-pc/gtfs-rt/vehicle-positions.pb"
TU_URL = "https://proxy.busone.app/seta-pc/gtfs-rt/trip-updates.pb"


def _vehicle_feed_bytes() -> bytes:
    feed = pb.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = 1000

    entity = feed.entity.add()
    entity.id = "5284"
    v = entity.vehicle
    v.trip.trip_id = "PC15-5795574"
    v.trip.route_id = "PC15"
    v.vehicle.id = "5284"
    v.position.latitude = 45.0521
    v.position.longitude = 9.6930
    v.timestamp = 999
    return feed.SerializeToString()


def _trip_update_feed_bytes() -> bytes:
    feed = pb.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = 1001

    entity = feed.entity.add()
    entity.id = "PC15-5795574"
    tu = entity.trip_update
    tu.trip.trip_id = "PC15-5795574"
    tu.trip.route_id = "PC15"
    tu.vehicle.id = "5284"
    stu = tu.stop_time_update.add()
    stu.stop_sequence = 4
    stu.stop_id = "PCU1002A"
    stu.arrival.time = 1_792_505_040
    stu.arrival.delay = 120
    return feed.SerializeToString()


@pytest.fixture
def settings(tmp_path):
    return replace(load_settings(), cache_dir=tmp_path)


@respx.mock
def test_fetch_vehicle_positions_sends_identification_headers(settings):
    route = respx.get(VP_URL).mock(httpx.Response(200, content=_vehicle_feed_bytes()))

    snapshot = fetch_vehicle_positions(settings)

    assert snapshot.feed_timestamp == 1000
    assert snapshot.vehicles[0].vehicle.vehicle_id == "5284"
    request = route.calls[0].request
    assert request.headers["x-busone-client"].startswith("segui_bus/")
    assert request.headers["x-busone-use"] == "personal"
    assert request.headers["user-agent"].startswith("segui_bus/")


@respx.mock
def test_fetch_trip_updates(settings):
    respx.get(TU_URL).mock(httpx.Response(200, content=_trip_update_feed_bytes()))

    snapshot = fetch_trip_updates(settings)

    update = snapshot.trip_updates[0]
    assert update.vehicle.vehicle_id == "5284"
    assert update.stop_time_updates[0].arrival_delay_s == 120


@respx.mock
def test_fetch_all_fetches_both_feeds(settings):
    respx.get(VP_URL).mock(httpx.Response(200, content=_vehicle_feed_bytes()))
    respx.get(TU_URL).mock(httpx.Response(200, content=_trip_update_feed_bytes()))

    vehicles, updates = fetch_all(settings)

    assert len(vehicles.vehicles) == 1
    assert len(updates.trip_updates) == 1


@respx.mock
def test_http_errors_are_raised(settings):
    respx.get(VP_URL).mock(httpx.Response(503))

    with pytest.raises(httpx.HTTPStatusError):
        fetch_vehicle_positions(settings)
