"""Static GTFS (gtfs.zip): download, cache, parse, look up.

The feed is published with an ETag and answers ``If-None-Match`` with 304, so
revalidating costs no bandwidth. The zip is kept on disk; parsing and every
lookup below are pure functions over a :class:`StaticFeed`.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import httpx

from .config import Settings
from .domain import GeoArea, GeoPoint, Route, ScheduledStop, StaticFeed, Stop, Trip
from .geo import contains, distance_m


class StaticFeedUnavailable(RuntimeError):
    """The static feed could not be downloaded and no cached copy exists."""


# --- cache on disk ---------------------------------------------------------


def _cache_paths(cache_dir: Path) -> tuple[Path, Path]:
    """The two files that make up the cache: the zip and its metadata."""
    return cache_dir / "gtfs.zip", cache_dir / "gtfs.meta.json"


def _read_meta(meta_path: Path) -> dict[str, str | None]:
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}  # a corrupt cache is treated as an empty one


def _atomic_write(path: Path, data: bytes) -> None:
    """Write via a temporary file, so a reader never sees a half-written zip."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


# --- loading (the only IO in this module) ----------------------------------


def load_static(settings: Settings) -> StaticFeed:
    """Return the current static feed, downloading it only when it changed.

    Preference order:
      1. 304 Not Modified -> the cached zip (the common case);
      2. 200 -> the new zip replaces the cache;
      3. any HTTP/network failure -> the cached zip, if there is one;
      4. failure with no cache -> StaticFeedUnavailable.
    """
    zip_path, meta_path = _cache_paths(settings.cache_dir)
    meta = _read_meta(meta_path)

    headers = settings.headers()
    if meta.get("etag"):
        headers["If-None-Match"] = str(meta["etag"])

    try:
        with httpx.Client(headers=headers, timeout=settings.fetch_timeout_s) as http:
            response = http.get(settings.gtfs_url)
            # 304 is not an error, everything 4xx/5xx is.
            response.raise_for_status()
    except httpx.HTTPError as exc:
        return _cached_or_raise(zip_path, meta, exc)

    if response.status_code == 304:
        return _cached_or_raise(zip_path, meta, None)

    fresh_meta = {
        "etag": response.headers.get("etag"),
        "last_modified": response.headers.get("last-modified"),
    }
    _atomic_write(zip_path, response.content)
    _atomic_write(meta_path, json.dumps(fresh_meta).encode("utf-8"))

    return parse_gtfs_zip(
        zip_path,
        etag=fresh_meta["etag"],
        last_modified=fresh_meta["last_modified"],
    )


def _cached_or_raise(zip_path: Path, meta: dict[str, str | None], error: Exception | None):
    """Use the cached zip; if there is none, say so plainly."""
    if not zip_path.exists():
        raise StaticFeedUnavailable("gtfs.zip unavailable and no cached copy") from error
    return parse_gtfs_zip(zip_path, etag=meta.get("etag"), last_modified=meta.get("last_modified"))


# --- parsing (pure) --------------------------------------------------------


def parse_gtfs_zip(
    path: Path, *, etag: str | None = None, last_modified: str | None = None
) -> StaticFeed:
    """Parse a gtfs.zip into an indexed StaticFeed."""
    with zipfile.ZipFile(path) as archive:
        stops = _parse_stops(_rows(archive, "stops.txt"))
        routes, routes_by_line = _parse_routes(_rows(archive, "routes.txt"))
        trips = _parse_trips(_rows(archive, "trips.txt"))
        trip_stops = _parse_stop_times(_rows(archive, "stop_times.txt"))

    return StaticFeed(
        etag=etag,
        last_modified=last_modified,
        stops=stops,
        routes=routes,
        routes_by_line=routes_by_line,
        trips=trips,
        trip_stops=trip_stops,
    )


def _rows(archive: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    """Stream one CSV member of the zip.

    Streamed rather than materialised: stop_times.txt has ~68k rows.
    utf-8-sig because the files carry a BOM; newline="" as csv requires.
    """
    with archive.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        yield from csv.DictReader(text)


def _int_or_none(value: str | None) -> int | None:
    return int(value) if value not in (None, "") else None


def _parse_stops(rows: Iterator[dict[str, str]]) -> dict[str, Stop]:
    return {
        row["stop_id"]: Stop(
            stop_id=row["stop_id"],
            name=row["stop_name"],
            point=GeoPoint(lat=float(row["stop_lat"]), lon=float(row["stop_lon"])),
        )
        for row in rows
    }


def _parse_routes(rows: Iterator[dict[str, str]]) -> tuple[dict[str, Route], dict[str, Route]]:
    """routes.txt -> by route_id and by human line number.

    In this feed the mapping is 1:1 (68 routes, 68 distinct short names); if a
    line ever repeats, the first route wins and the caller iterates routes
    instead.
    """
    routes: dict[str, Route] = {}
    routes_by_line: dict[str, Route] = {}

    for row in rows:
        route = Route(
            route_id=row["route_id"],
            line=row["route_short_name"],
            long_name=row.get("route_long_name") or None,
            color=row.get("route_color") or None,
        )
        routes[route.route_id] = route
        routes_by_line.setdefault(route.line, route)

    return routes, routes_by_line


def _parse_trips(rows: Iterator[dict[str, str]]) -> dict[str, Trip]:
    return {
        row["trip_id"]: Trip(
            trip_id=row["trip_id"],
            route_id=row["route_id"],
            direction_id=_int_or_none(row.get("direction_id")),
            headsign=row.get("trip_headsign") or None,
        )
        for row in rows
    }


def _parse_stop_times(rows: Iterator[dict[str, str]]) -> dict[str, tuple[ScheduledStop, ...]]:
    """stop_times.txt -> stops per trip, ordered by stop_sequence.

    The file happens to be ordered already; sorting keeps us independent of it.
    """
    grouped: dict[str, list[ScheduledStop]] = {}

    for row in rows:
        grouped.setdefault(row["trip_id"], []).append(
            ScheduledStop(
                stop_id=row["stop_id"],
                stop_sequence=int(row["stop_sequence"]),
                arrival_time=row["arrival_time"],
            )
        )

    return {
        trip_id: tuple(sorted(stops, key=lambda scheduled: scheduled.stop_sequence))
        for trip_id, stops in grouped.items()
    }


# --- lookups (pure) --------------------------------------------------------


def route_of_line(feed: StaticFeed, line: str) -> Route | None:
    """The line a rider names, e.g. "15" -> Route(route_id="PC15")."""
    return feed.routes_by_line.get(line.strip())


def line_of_route(feed: StaticFeed, route_id: str) -> str | None:
    """The inverse mapping, used to fill TripRef.line on realtime data."""
    route = feed.routes.get(route_id)
    return route.line if route else None


def find_stops_by_name(feed: StaticFeed, text: str) -> list[Stop]:
    """Case-insensitive substring search over stop names, e.g. "marconi"."""
    needle = text.strip().casefold()
    return [stop for stop in feed.stops.values() if needle in stop.name.casefold()]


def nearest_stop(feed: StaticFeed, point: GeoPoint, radius: float | None = None) -> Stop | None:
    """The closest stop, optionally only if within ``radius`` metres."""
    best: Stop | None = None
    best_distance = float("inf")

    for stop in feed.stops.values():
        distance = distance_m(point, stop.point)
        if distance < best_distance:
            best, best_distance = stop, distance

    if best is None or (radius is not None and best_distance > radius):
        return None
    return best


def stops_in_area(feed: StaticFeed, area: GeoArea) -> list[Stop]:
    """Stops inside the fence, nearest first."""
    inside = [stop for stop in feed.stops.values() if contains(area, stop.point)]
    return sorted(inside, key=lambda stop: distance_m(area.point, stop.point))
