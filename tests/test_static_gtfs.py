"""Static GTFS tests.

A tiny synthetic gtfs.zip is written to a temp directory, so parsing is tested
without the network and without a 3.2 MB fixture. The cache behaviour is tested
with respx, which fakes httpx at the transport level.
"""

import zipfile
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx

from segui_bus.config import load_settings
from segui_bus.domain import GeoPoint
from segui_bus.geo import around
from segui_bus.static_gtfs import (
    StaticFeedUnavailable,
    find_stops_by_name,
    line_of_route,
    load_static,
    nearest_stop,
    parse_gtfs_zip,
    route_of_line,
    stops_in_area,
)

GTFS_URL = "https://proxy.busone.app/seta-pc/gtfs.zip"

# Note the quoted route_long_name: the real file has commas inside fields.
STOPS = """stop_id,stop_name,stop_lat,stop_lon
S1,"P.le Marconi, 20 (Stazione Fs corsia 1)",45.052069,9.705346
S2,"P.le Marconi, 20 (Stazione Fs corsia 2)",45.052135,9.705325
S3,"Ferrovia, corsia 2",45.060000,9.700000
S4,CROCETTA,45.100000,9.800000
"""

ROUTES = """route_id,route_short_name,route_long_name,route_color
PC15,15,Via Roma - Ferrovia,E3001B
PC1,1,"Ferrovia - belvedere - Ferrovia",32CD32
"""

TRIPS = """route_id,service_id,trip_id,trip_headsign,direction_id
PC15,SVC,PC15-1,Ferrovia,0
PC15,SVC,PC15-2,Marconi,1
"""

# Deliberately out of order: PC15-1 lists stop_sequence 2 before 1.
STOP_TIMES = """trip_id,arrival_time,departure_time,stop_id,stop_sequence
PC15-1,19:40:00,19:40:00,S3,2
PC15-1,19:32:00,19:32:00,S1,1
PC15-2,19:45:00,19:45:00,S1,2
PC15-2,19:32:00,19:32:00,S3,1
"""


def _write_zip(path: Path) -> Path:
    files = {
        "stops.txt": STOPS,
        "routes.txt": ROUTES,
        "trips.txt": TRIPS,
        "stop_times.txt": STOP_TIMES,
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return path


@pytest.fixture
def static_zip(tmp_path: Path) -> Path:
    return _write_zip(tmp_path / "gtfs.zip")


@pytest.fixture
def settings(tmp_path: Path):
    """Real settings, pointed at a throwaway cache directory."""
    return replace(load_settings(), cache_dir=tmp_path / "cache")


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
