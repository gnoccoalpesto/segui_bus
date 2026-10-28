"""Static GTFS tests.

A tiny synthetic gtfs.zip is written to a temp directory, so parsing is tested
without the network and without a 3.2 MB fixture. The cache behaviour is tested
with respx, which fakes httpx at the transport level.
"""

from pathlib import Path

import httpx
import pytest
import respx

from segui_bus.domain import GeoPoint
from segui_bus.geo import around
from segui_bus.static_gtfs import (
    StaticFeedUnavailable,
    find_stops_by_name,
    is_date_covered,
    line_of_route,
    load_static,
    nearest_stop,
    parse_gtfs_zip,
    route_of_line,
    scheduled_stop,
    stops_in_area,
    trips_running_on,
)

GTFS_URL = "https://proxy.busone.app/seta-pc/gtfs.zip"


# --- parsing ---------------------------------------------------------------


def test_parses_stops(static_zip: Path):
    feed = parse_gtfs_zip(static_zip, etag='"abc"', last_modified="Thu, 24 Sep 2026 08:36:34 GMT")

    assert feed.etag == '"abc"'
    assert len(feed.stops) == 4
    assert feed.stops["S1"].point.lat == pytest.approx(45.052069)
    # A comma inside a quoted field must survive.
    assert feed.stops["S1"].name == "P.le Marconi, 20 (Stazione Fs corsia 1)"
    assert feed.stops["S3"].name == "Ferrovia, corsia 2"


def test_parses_routes_by_route_id_and_by_line(static_zip: Path):
    feed = parse_gtfs_zip(static_zip)

    assert feed.routes["PC15"].line == "15"
    assert feed.routes["PC1"].long_name == "Ferrovia - belvedere - Ferrovia"
    # Both indexes point at the same Route objects.
    assert feed.routes_by_line["15"] is feed.routes["PC15"]


def test_parses_trips(static_zip: Path):
    feed = parse_gtfs_zip(static_zip)

    assert feed.trips["PC15-2"].route_id == "PC15"
    assert feed.trips["PC15-2"].direction_id == 1
    assert feed.trips["PC15-1"].headsign == "Ferrovia"


def test_stop_times_are_ordered_by_stop_sequence(static_zip: Path):
    feed = parse_gtfs_zip(static_zip)

    scheduled = feed.trip_stops["PC15-1"]
    assert [s.stop_id for s in scheduled] == ["S1", "S3"]
    assert scheduled[0].arrival_time == "19:32:00"
    assert [s.stop_sequence for s in scheduled] == [1, 2]


def test_scheduled_times_are_keyed_by_sequence_not_by_stop_id(static_zip: Path):
    """PC15-3 visits S1 twice: the two visits have different times."""
    feed = parse_gtfs_zip(static_zip)

    first_visit = scheduled_stop(feed, "PC15-3", 1)
    second_visit = scheduled_stop(feed, "PC15-3", 3)

    assert first_visit.stop_id == second_visit.stop_id == "S1"
    assert first_visit.arrival_time == "21:00:00"
    assert second_visit.arrival_time == "21:12:00"
    assert scheduled_stop(feed, "PC15-3", 99) is None


# --- calendar --------------------------------------------------------------


def test_parses_calendar_dates(static_zip: Path):
    feed = parse_gtfs_zip(static_zip)

    assert feed.service_dates["20261022"] == frozenset({"SVC", "OTHER"})
    assert feed.service_dates["20261028"] == frozenset({"SVC"})
    # Added and then removed on the same date: it must disappear.
    assert "20261029" not in feed.service_dates
    assert feed.calendar_range == ("20261022", "20261028")


def test_is_date_covered(static_zip: Path):
    feed = parse_gtfs_zip(static_zip)

    assert is_date_covered(feed, "20261022")
    assert is_date_covered(feed, "20261028")
    assert not is_date_covered(feed, "20261101")  # past the range
    assert not is_date_covered(feed, "20261001")  # before it


def test_trips_running_on(static_zip: Path):
    feed = parse_gtfs_zip(static_zip)

    assert trips_running_on(feed, "20261028") == {"PC15-1", "PC15-2", "PC15-3", "PC1-1"}
    assert trips_running_on(feed, "20261029") == set()  # uncovered date


# --- lookups ---------------------------------------------------------------


def test_route_of_line_and_back(static_zip: Path):
    feed = parse_gtfs_zip(static_zip)

    assert route_of_line(feed, "15").route_id == "PC15"
    assert route_of_line(feed, " 15 ").route_id == "PC15"  # forgiving input
    assert route_of_line(feed, "99") is None
    assert line_of_route(feed, "PC15") == "15"
    assert line_of_route(feed, "nope") is None


def test_find_stops_by_name_is_case_insensitive(static_zip: Path):
    feed = parse_gtfs_zip(static_zip)

    found = {stop.stop_id for stop in find_stops_by_name(feed, "marconi")}
    assert found == {"S1", "S2"}
    assert find_stops_by_name(feed, "nothing like this") == []


def test_nearest_stop_and_optional_radius(static_zip: Path):
    feed = parse_gtfs_zip(static_zip)
    point = GeoPoint(lat=45.052100, lon=9.705300)  # ~4.4 m from S2, ~5.0 m from S1

    assert nearest_stop(feed, point).stop_id == "S2"
    assert nearest_stop(feed, point, radius=10.0).stop_id == "S2"
    assert nearest_stop(feed, point, radius=1.0) is None  # nothing that close


def test_stops_in_area_returns_nearest_first(static_zip: Path):
    feed = parse_gtfs_zip(static_zip)

    # A 30 m fence around S1 catches the second platform of the same stop only.
    area = around(feed.stops["S1"].point, 30.0)

    assert [stop.stop_id for stop in stops_in_area(feed, area)] == ["S1", "S2"]


# --- download and cache ----------------------------------------------------


@respx.mock
def test_downloads_once_then_revalidates_with_the_stored_etag(settings, static_zip: Path):
    route = respx.get(GTFS_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                content=static_zip.read_bytes(),
                headers={"etag": '"abc123"', "last-modified": "Thu, 24 Sep 2026 08:36:34 GMT"},
            ),
            httpx.Response(304),
        ]
    )

    first = load_static(settings)
    second = load_static(settings)

    assert len(route.calls) == 2
    # The second request is a revalidation, not a re-download.
    assert route.calls[1].request.headers["if-none-match"] == '"abc123"'
    assert first.etag == '"abc123"'
    assert second.etag == '"abc123"'
    assert set(second.stops) == set(first.stops)
    # The zip is cached on disk, next to its metadata.
    assert (settings.cache_dir / "gtfs.zip").exists()
    assert (settings.cache_dir / "gtfs.meta.json").exists()


@respx.mock
def test_falls_back_to_the_cache_when_the_network_fails(settings, static_zip: Path):
    respx.get(GTFS_URL).mock(
        side_effect=[
            httpx.Response(200, content=static_zip.read_bytes(), headers={"etag": '"abc123"'}),
            httpx.ConnectError("network is down"),
        ]
    )

    load_static(settings)  # fills the cache
    degraded = load_static(settings)  # survives the outage

    assert len(degraded.stops) == 4


@respx.mock
def test_raises_when_there_is_no_network_and_no_cache(settings):
    respx.get(GTFS_URL).mock(side_effect=httpx.ConnectError("network is down"))

    with pytest.raises(StaticFeedUnavailable):
        load_static(settings)
