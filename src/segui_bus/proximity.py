"""How far away a bus is from a stop: in metres, in stops and in time.

Three independent answers, deliberately kept apart so the report can show all
of them and let the rider judge:

* ``distance_to_stop`` and ``in_fence`` use the reported position only;
* ``stops_to_target`` uses the itinerary and is immune to delay, DST and the
  service-day clock;
* ``eta_to_stop`` prefers the realtime estimate and falls back to the
  schedule, allowing negative delays for early running.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .clock import absolute_epoch, parse_service_date
from .domain import StaticFeed, Stop, StopTimeUpdate, TripUpdate, VehiclePosition
from .geo import around, contains, distance_m
from .progress import Progress


@dataclass(frozen=True, slots=True)
class Eta:
    """When a trip is expected at one stop, and where the number came from.

    ``source`` is "realtime" when trip-updates knew this stop, "schedule" when
    only gtfs.zip did. A negative ``delay_s`` means the bus is running early.
    """

    stop_id: str
    stop_sequence: int
    scheduled_unix: int
    expected_unix: int
    delay_s: int | None
    source: str
    seconds_from_now: int


def eta_to_stop(
    feed: StaticFeed,
    trip_id: str,
    stop_sequence: int,
    *,
    service_date: str,
    update: TripUpdate | None = None,
    now: float | None = None,
) -> Eta | None:
    """Estimate the arrival at a stop, keyed by sequence (never by stop_id).

    ``service_date`` is the GTFS Date of the run ("20261028"), normally taken
    from the trip descriptor; without it the schedule cannot be placed on the
    calendar and this returns None.
    """
    scheduled = feed.scheduled_stop(trip_id, stop_sequence)
    if scheduled is None:
        return None

    scheduled_unix = absolute_epoch(parse_service_date(service_date), scheduled.arrival_time)
    current = int(time.time()) if now is None else int(now)

    stop_update = _stop_time_update(update, trip_id, stop_sequence)
    if stop_update is not None and stop_update.arrival_time is not None:
        # An absolute Unix time needs no timezone or service-day arithmetic.
        expected, delay = stop_update.arrival_time, stop_update.arrival_delay_s
        if delay is None:
            delay = expected - scheduled_unix
        source = "realtime"
    elif stop_update is not None and stop_update.arrival_delay_s is not None:
        # Early buses produce negative delays: keep the sign, never clamp.
        delay = stop_update.arrival_delay_s
        expected, source = scheduled_unix + delay, "realtime"
    else:
        expected, delay, source = scheduled_unix, None, "schedule"

    return Eta(
        stop_id=scheduled.stop_id,
        stop_sequence=stop_sequence,
        scheduled_unix=scheduled_unix,
        expected_unix=expected,
        delay_s=delay,
        source=source,
        seconds_from_now=expected - current,
    )


def _stop_time_update(
    update: TripUpdate | None, trip_id: str, stop_sequence: int
) -> StopTimeUpdate | None:
    """The update for this trip and sequence; another trip's update is ignored."""
    if update is None or update.trip.trip_id != trip_id:
        return None
    return next(
        (
            stop_update
            for stop_update in update.stop_time_updates
            if stop_update.stop_sequence == stop_sequence
        ),
        None,
    )


def distance_to_stop(vehicle: VehiclePosition, stop: Stop) -> float | None:
    """Straight-line metres from the bus to the stop, or None without a fix."""
    if vehicle.position is None:
        return None
    return distance_m(vehicle.position, stop.point)


def in_fence(vehicle: VehiclePosition, stop: Stop, radius: float) -> bool:
    """Has the bus entered the circle around the stop? Inclusive at the edge."""
    if vehicle.position is None:
        return False
    return contains(around(stop.point, radius), vehicle.position)


def stops_to_target(feed: StaticFeed, progress: Progress, stop_id: str) -> int | None:
    """How many stops before the bus reaches ``stop_id`` on this trip.

    Returns 0 when the bus is standing at it, a positive count when the stop is
    still ahead, and None when the trip does not serve it any more (a loop line
    is searched from the current visit, not from the first one).
    """
    stops = feed.trip_stops.get(progress.trip_id)
    if not stops or progress.served_index is None or progress.next_index is None:
        return None

    # Normally the target must still be ahead; if the bus is standing at a
    # stop, that stop itself counts as distance zero.
    start = progress.served_index + 1
    if progress.next_index == progress.served_index:
        start = progress.served_index

    for index in range(max(start, 0), len(stops)):
        if stops[index].stop_id == stop_id:
            return index - progress.served_index
    return None
