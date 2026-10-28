"""Joins between what the rider asks for and the objects in both feeds.

The rider names a line ("15") and a stop ("Marconi"). The realtime feeds speak
route_id ("PC15"), trip_id and stop_id. This module is the thin translation
layer between the two, so nothing downstream has to know both vocabularies.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from .domain import GeoPoint, StaticFeed, Stop, TripRef, VehiclePosition, VehicleSnapshot
from .geo import distance_m
from .static_gtfs import route_of_line


def vehicles_for_line(
    feed: StaticFeed, snapshot: VehicleSnapshot, line: str
) -> tuple[VehiclePosition, ...]:
    """The vehicles currently running trips of one line.

    A vehicle without a trip cannot be attributed to a line, and is skipped.
    An unknown line yields nothing rather than raising: callers that need to
    report "unknown line" should call route_of_line first.
    """
    route = route_of_line(feed, line)
    if route is None:
        return ()
    return tuple(
        vehicle
        for vehicle in snapshot.vehicles
        if vehicle.trip is not None and vehicle.trip.route_id == route.route_id
    )


def stop_ids_of_line(feed: StaticFeed, line: str) -> frozenset[str]:
    """Every stop that any trip of the line ever serves (both directions)."""
    route = route_of_line(feed, line)
    if route is None:
        return frozenset()

    return frozenset(
        scheduled.stop_id
        for trip_id, trip in feed.trips.items()
        if trip.route_id == route.route_id
        for scheduled in feed.trip_stops.get(trip_id, ())
    )


def stops_of_line(feed: StaticFeed, line: str) -> tuple[Stop, ...]:
    """The line's stops as Stop objects, ordered by name for stable output."""
    served = stop_ids_of_line(feed, line)
    stops = (feed.stops[stop_id] for stop_id in served if stop_id in feed.stops)
    return tuple(sorted(stops, key=lambda stop: stop.name))


def stop_at(feed: StaticFeed, stop_id: str) -> Stop | None:
    """One stop by its identifier, or None."""
    return feed.stops.get(stop_id)


def resolve_stops(feed: StaticFeed, line: str, query: str) -> list[Stop]:
    """Find the stop the rider means, among the stops the line serves.

    Accepted forms, in order of precedence:
      * "45.0521,9.7053" -> the line's stops, nearest first;
      * an exact stop_id   -> that stop;
      * any text           -> case-insensitive substring of the name.

    Filtering through the line first is what makes "Marconi" unambiguous when
    two platforms of the same interchange are served by different lines.
    """
    text = query.strip()
    served = stops_of_line(feed, line)

    point = _parse_point(text)
    if point is not None:
        return sorted(served, key=lambda stop: distance_m(point, stop.point))

    exact = [stop for stop in served if stop.stop_id == text]
    if exact:
        return exact

    needle = text.casefold()
    return [stop for stop in served if needle in stop.name.casefold()]


def _parse_point(text: str) -> GeoPoint | None:
    """Parse "lat,lon" into a GeoPoint; anything else yields None, not an error."""
    latitude, separator, longitude = text.partition(",")
    if not separator:
        return None
    try:
        return GeoPoint(lat=float(latitude), lon=float(longitude))
    except ValueError:
        return None


def enrich_lines(feed: StaticFeed, trips: Iterable[TripRef]) -> tuple[TripRef, ...]:
    """Fill TripRef.line (and a missing direction) from trips.txt.

    Realtime feeds omit the human line number, so every report needs this join.
    Unknown trips are returned unchanged rather than dropped.
    """
    return tuple(_enrich_trip(feed, trip) for trip in trips)


def _enrich_trip(feed: StaticFeed, trip: TripRef) -> TripRef:
    scheduled = feed.trips.get(trip.trip_id)
    if scheduled is None:
        return trip

    route = feed.routes.get(scheduled.route_id)
    return replace(
        trip,
        route_id=trip.route_id or scheduled.route_id,
        line=route.line if route else trip.line,
        direction_id=(
            trip.direction_id if trip.direction_id is not None else scheduled.direction_id
        ),
    )
