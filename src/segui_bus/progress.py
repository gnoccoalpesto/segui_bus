"""Where a bus is along its itinerary, and how much of the route is left.

Two facts make this less obvious than it looks:

* ``current_stop_sequence`` is 1-based and anchors the bus on the itinerary;
  ``STOPPED_AT`` means "at that stop", the other statuses mean "heading to it".
  The two cases differ by one stop, and that is the difference between firing
  an alert and missing it.
* loop lines visit the same ``stop_id`` twice in one trip, so the anchor is
  always the sequence, never the id.

Distances are measured along the itinerary (haversine between consecutive
scheduled stops). ``shapes.txt`` is not downloaded, so this is an
approximation, but it is monotonic and good enough to compare two buses or to
show a percentage.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise

from .domain import GeoPoint, ScheduledStop, StaticFeed, VehiclePosition
from .geo import distance_m

STOPPED_AT = "STOPPED_AT"


@dataclass(frozen=True, slots=True)
class Progress:
    """One vehicle's position on its trip, in stops and in metres.

    ``served_index`` is the last stop the bus has left (or is standing at);
    it is -1 before the first stop. ``next_index`` is the stop it is heading
    to, or the one it is standing at. ``None`` means the feed gave no usable
    anchor (no sequence, no stop and no position).

    ``stops_left`` counts the stops still ahead, including the next one.
    ``distance_done_m`` is measured to the last served stop, so a bus between
    two stops is credited with the earlier one.
    """

    trip_id: str
    next_index: int | None
    served_index: int | None
    next_stop_id: str | None
    next_stop_sequence: int | None
    served_stop_id: str | None
    stops_total: int
    stops_left: int
    distance_done_m: float
    distance_left_m: float
    route_length_m: float
    fraction: float
    # Straight-line from the vehicle's reported position to the termini.
    to_first_stop_m: float | None
    to_last_stop_m: float | None


def progress_for(feed: StaticFeed, vehicle: VehiclePosition) -> Progress | None:
    """Build the progress of one vehicle, or None if its trip is unknown."""
    if vehicle.trip is None:
        return None

    stops = feed.trip_stops.get(vehicle.trip.trip_id)
    if not stops:
        return None

    points = _points(feed, stops)
    cumulative = _cumulative(points)
    next_index = _next_index(stops, points, vehicle)

    if next_index is None:
        return Progress(
            trip_id=vehicle.trip.trip_id,
            next_index=None,
            served_index=None,
            next_stop_id=None,
            next_stop_sequence=None,
            served_stop_id=None,
            stops_total=len(stops),
            stops_left=len(stops),
            distance_done_m=0.0,
            distance_left_m=cumulative[-1],
            route_length_m=cumulative[-1],
            fraction=0.0,
            to_first_stop_m=_straight(vehicle.position, points[0]),
            to_last_stop_m=_straight(vehicle.position, points[-1]),
        )

    # STOPPED_AT: the sequence is the stop being served, so it counts as done.
    served_index = next_index if vehicle.current_status == STOPPED_AT else next_index - 1
    done = cumulative[served_index] if served_index >= 0 else 0.0
    length = cumulative[-1]

    return Progress(
        trip_id=vehicle.trip.trip_id,
        next_index=next_index,
        served_index=served_index,
        next_stop_id=stops[next_index].stop_id,
        next_stop_sequence=stops[next_index].stop_sequence,
        served_stop_id=stops[served_index].stop_id if served_index >= 0 else None,
        stops_total=len(stops),
        stops_left=max(len(stops) - served_index - 1, 0),
        distance_done_m=done,
        distance_left_m=length - done,
        route_length_m=length,
        fraction=done / length if length else 0.0,
        to_first_stop_m=_straight(vehicle.position, points[0]),
        to_last_stop_m=_straight(vehicle.position, points[-1]),
    )


def _points(feed: StaticFeed, stops: tuple[ScheduledStop, ...]) -> tuple[GeoPoint, ...]:
    """The scheduled stop coordinates; a stop missing from stops.txt is skipped.

    In practice every stop_time refers to a known stop, so the assert-style
    fallback is only here to keep the itinerary aligned.
    """
    return tuple(
        feed.stops[stop.stop_id].point if stop.stop_id in feed.stops else GeoPoint(0.0, 0.0)
        for stop in stops
    )


@lru_cache(maxsize=2048)
def _cumulative(points: tuple[GeoPoint, ...]) -> tuple[float, ...]:
    """Distance from the first stop to each stop, along the itinerary.

    Cached by the geometry itself (the points are hashable), which is the same
    as caching per (feed build, trip) without holding on to a feed instance.
    """
    totals = [0.0]
    for previous, current in pairwise(points):
        totals.append(totals[-1] + distance_m(previous, current))
    return tuple(totals)


def _next_index(
    stops: tuple[ScheduledStop, ...], points: tuple[GeoPoint, ...], vehicle: VehiclePosition
) -> int | None:
    """Index of the stop the packet points at, with the research note's fallbacks.

    Preference: current_stop_sequence (1-based, authoritative), then stop_id,
    then the closest scheduled stop to the reported position. For a loop line
    the stop_id fallback picks the occurrence nearest the bus.
    """
    sequence = vehicle.current_stop_sequence
    if sequence is not None:
        for index, stop in enumerate(stops):
            if stop.stop_sequence == sequence:
                return index

    if vehicle.stop_id is not None:
        matches = [index for index, stop in enumerate(stops) if stop.stop_id == vehicle.stop_id]
        if matches:
            if vehicle.position is None:
                return matches[0]
            return min(matches, key=lambda index: distance_m(vehicle.position, points[index]))

    if vehicle.position is not None:
        return min(range(len(stops)), key=lambda index: distance_m(vehicle.position, points[index]))

    return None


def _straight(origin: GeoPoint | None, destination: GeoPoint) -> float | None:
    return None if origin is None else distance_m(origin, destination)
