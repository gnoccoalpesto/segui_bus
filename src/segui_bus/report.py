"""The objects a report is made of, and the pure functions that build them.

A report is a snapshot of one fetch, never a merged one: the rider sees every
vehicle of the line with the chosen one marked, plus the freshness of each
feed. Selection is a separate step, so the same data can be shown before and
after choosing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from .clock import TIMEZONE, local_clock
from .domain import (
    GeoPoint,
    StaticFeed,
    Stop,
    TripUpdate,
    TripUpdateSnapshot,
    VehiclePosition,
    VehicleSnapshot,
)
from .lookup import enrich_lines, vehicles_for_line
from .progress import Progress, progress_for
from .proximity import distance_to_stop, eta_to_stop, in_fence, stops_to_target
from .static_gtfs import is_date_covered, route_of_line

STRATEGIES = ("fence", "schedule", "nearest")


@dataclass(frozen=True, slots=True)
class Candidate:
    """One vehicle of the line, with everything known about its relation to the trip.

    ``target_index`` is the visit of the wanted stop the bus can still reach;
    ``progress_index`` is the last stop it has served. ``stops_to_target`` is
    ``target_index - progress_index`` and is None when the stop is behind it.
    """

    vehicle_id: str
    trip_id: str
    route_id: str
    line: str | None
    direction_id: int | None
    headsign: str | None
    start_time: str | None
    service_date: str | None
    current_status: str | None
    position: GeoPoint | None
    serves_stop: bool
    target_index: int | None
    progress_index: int | None
    stops_to_target: int | None
    distance_m: float | None
    in_fence: bool
    scheduled_unix: int | None
    arrival_unix: int | None
    delay_s: int | None
    eta_source: str | None
    stale: bool
    progress: Progress | None


@dataclass(frozen=True, slots=True)
class FeedStatus:
    """How much to trust the two realtime feeds right now."""

    service_date: str
    calendar_covered: bool
    vehicle_age_s: float
    update_age_s: float
    stale: bool


@dataclass(frozen=True, slots=True)
class LineReport:
    line: str
    route_id: str | None
    feed: FeedStatus
    strategy: str
    candidates: tuple[Candidate, ...]
    selected: Candidate | None


@dataclass(frozen=True, slots=True)
class StopReport:
    line: str
    route_id: str | None
    stop: Stop
    radius_m: float
    feed: FeedStatus
    strategy: str
    candidates: tuple[Candidate, ...]
    selected: Candidate | None


@dataclass(frozen=True, slots=True)
class ItineraryStop:
    """One row of a vehicle's itinerary, with its scheduled and expected time."""

    index: int
    stop_sequence: int
    stop_id: str
    name: str
    scheduled_time: str
    scheduled_unix: int | None
    expected_unix: int | None
    delay_s: int | None
    source: str | None
    served: bool
    next: bool


@dataclass(frozen=True, slots=True)
class VehicleReport:
    vehicle_id: str
    line: str | None
    route_id: str | None
    trip_id: str | None
    direction_id: int | None
    headsign: str | None
    start_time: str | None
    service_date: str | None
    feed: FeedStatus
    candidate: Candidate | None
    itinerary: tuple[ItineraryStop, ...]


# --- building --------------------------------------------------------------


def line_report(
    feed: StaticFeed,
    vehicles: VehicleSnapshot,
    updates: TripUpdateSnapshot,
    line: str,
    *,
    now: float | None = None,
    stale_after_s: int = 120,
    strategy: str = "nearest",
) -> LineReport:
    """Every vehicle currently running the line, no stop involved."""
    current = _now(now)
    route = route_of_line(feed, line)
    candidates = _candidates(
        feed,
        vehicles,
        updates,
        line=line,
        stop=None,
        radius_m=0.0,
        now=current,
        stale_after_s=stale_after_s,
    )
    service_date = candidates[0].service_date if candidates else _today(current)

    return LineReport(
        line=line,
        route_id=route.route_id if route else None,
        feed=_feed_status(feed, vehicles, updates, current, stale_after_s, service_date),
        strategy=strategy,
        candidates=candidates,
        selected=select_vehicle(candidates, strategy),
    )


def stop_report(
    feed: StaticFeed,
    vehicles: VehicleSnapshot,
    updates: TripUpdateSnapshot,
    line: str,
    stop: Stop,
    *,
    radius_m: float = 150.0,
    now: float | None = None,
    stale_after_s: int = 120,
    strategy: str = "fence",
) -> StopReport:
    """Every vehicle of the line, with its relation to one stop."""
    current = _now(now)
    route = route_of_line(feed, line)
    candidates = _candidates(
        feed,
        vehicles,
        updates,
        line=line,
        stop=stop,
        radius_m=radius_m,
        now=current,
        stale_after_s=stale_after_s,
    )
    service_date = candidates[0].service_date if candidates else _today(current)

    return StopReport(
        line=line,
        route_id=route.route_id if route else None,
        stop=stop,
        radius_m=radius_m,
        feed=_feed_status(feed, vehicles, updates, current, stale_after_s, service_date),
        strategy=strategy,
        candidates=candidates,
        selected=select_vehicle(candidates, strategy),
    )


def vehicle_report(
    feed: StaticFeed,
    vehicles: VehicleSnapshot,
    updates: TripUpdateSnapshot,
    vehicle_id: str,
    *,
    stop: Stop | None = None,
    radius_m: float = 150.0,
    now: float | None = None,
    stale_after_s: int = 120,
) -> VehicleReport | None:
    """One bus, its progress and its whole itinerary. None if it is not running."""
    current = _now(now)
    vehicle = vehicles.by_id(vehicle_id)
    if vehicle is None:
        return None

    stale = _is_stale(vehicles.received_at, current, stale_after_s) or _is_stale(
        updates.received_at, current, stale_after_s
    )
    update = _update_for(updates, vehicle)
    candidate = None
    if vehicle.trip is not None:
        candidate = _candidate(
            feed,
            vehicle,
            update,
            stop=stop,
            radius_m=radius_m,
            now=current,
            stale=stale,
        )
    service_date = (candidate.service_date if candidate else None) or _today(current)
    trip_id = candidate.trip_id if candidate else ""
    trip_schedule = feed.trips.get(trip_id)

    return VehicleReport(
        vehicle_id=vehicle_id,
        line=candidate.line if candidate else None,
        route_id=candidate.route_id if candidate else None,
        trip_id=trip_id or None,
        direction_id=candidate.direction_id if candidate else None,
        headsign=trip_schedule.headsign if trip_schedule else None,
        start_time=candidate.start_time if candidate else None,
        service_date=candidate.service_date if candidate else None,
        feed=_feed_status(feed, vehicles, updates, current, stale_after_s, service_date),
        candidate=candidate,
        itinerary=_itinerary(
            feed,
            update,
            trip_id,
            service_date,
            candidate.progress if candidate else None,
            current,
        ),
    )


def _candidates(
    feed: StaticFeed,
    vehicles: VehicleSnapshot,
    updates: TripUpdateSnapshot,
    *,
    line: str,
    stop: Stop | None,
    radius_m: float,
    now: int,
    stale_after_s: int,
) -> tuple[Candidate, ...]:
    stale = _is_stale(vehicles.received_at, now, stale_after_s) or _is_stale(
        updates.received_at, now, stale_after_s
    )
    built = (
        _candidate(
            feed,
            vehicle,
            _update_for(updates, vehicle),
            stop=stop,
            radius_m=radius_m,
            now=now,
            stale=stale,
        )
        for vehicle in vehicles_for_line(feed, vehicles, line)
    )
    return tuple(sorted(built, key=lambda candidate: candidate.vehicle_id))


def _candidate(
    feed: StaticFeed,
    vehicle: VehiclePosition,
    update: TripUpdate | None,
    *,
    stop: Stop | None,
    radius_m: float,
    now: int,
    stale: bool,
) -> Candidate:
    trip = vehicle.trip
    assert trip is not None  # vehicles_for_line only yields vehicles with a trip

    (enriched,) = enrich_lines(feed, [trip])
    scheduled_trip = feed.trips.get(trip.trip_id)
    service_date = (
        enriched.start_date or (update.trip.start_date if update else None) or _today(now)
    )
    progress = progress_for(feed, vehicle)

    serves_stop = False
    target_index: int | None = None
    stops_to: int | None = None
    if stop is not None and progress is not None:
        stops = feed.trip_stops.get(trip.trip_id, ())
        serves_stop = any(scheduled.stop_id == stop.stop_id for scheduled in stops)
        if serves_stop:
            stops_to = stops_to_target(feed, progress, stop.stop_id)
            if stops_to is not None and progress.served_index is not None:
                target_index = progress.served_index + stops_to

    scheduled_unix = arrival_unix = delay_s = None
    eta_source = None
    if stop is not None and target_index is not None:
        stops = feed.trip_stops.get(trip.trip_id, ())
        sequence = stops[target_index].stop_sequence
        eta = eta_to_stop(
            feed,
            trip.trip_id,
            sequence,
            service_date=service_date,
            update=update,
            now=now,
        )
        if eta is not None:
            scheduled_unix = eta.scheduled_unix
            arrival_unix = eta.expected_unix
            delay_s = eta.delay_s
            eta_source = eta.source

    return Candidate(
        vehicle_id=vehicle.vehicle.vehicle_id,
        trip_id=trip.trip_id,
        route_id=enriched.route_id,
        line=enriched.line,
        direction_id=enriched.direction_id,
        headsign=scheduled_trip.headsign if scheduled_trip else None,
        start_time=enriched.start_time,
        service_date=service_date,
        current_status=vehicle.current_status,
        position=vehicle.position,
        serves_stop=serves_stop,
        target_index=target_index,
        progress_index=progress.served_index if progress else None,
        stops_to_target=stops_to,
        distance_m=distance_to_stop(vehicle, stop) if stop else None,
        in_fence=in_fence(vehicle, stop, radius_m) if stop else False,
        scheduled_unix=scheduled_unix,
        arrival_unix=arrival_unix,
        delay_s=delay_s,
        eta_source=eta_source,
        stale=stale,
        progress=progress,
    )


def _itinerary(
    feed: StaticFeed,
    update: TripUpdate | None,
    trip_id: str,
    service_date: str,
    progress: Progress | None,
    now: int,
) -> tuple[ItineraryStop, ...]:
    rows = []
    for index, scheduled in enumerate(feed.trip_stops.get(trip_id, ())):
        eta = eta_to_stop(
            feed,
            trip_id,
            scheduled.stop_sequence,
            service_date=service_date,
            update=update,
            now=now,
        )
        stop = feed.stops.get(scheduled.stop_id)
        rows.append(
            ItineraryStop(
                index=index,
                stop_sequence=scheduled.stop_sequence,
                stop_id=scheduled.stop_id,
                name=stop.name if stop else scheduled.stop_id,
                scheduled_time=scheduled.arrival_time,
                scheduled_unix=eta.scheduled_unix if eta else None,
                expected_unix=eta.expected_unix if eta else None,
                delay_s=eta.delay_s if eta else None,
                source=eta.source if eta else None,
                served=progress is not None
                and progress.served_index is not None
                and index <= progress.served_index,
                next=progress is not None and progress.next_index == index,
            )
        )
    return tuple(rows)


def _update_for(updates, vehicle: VehiclePosition) -> TripUpdate | None:
    """The trip update for this vehicle: by trip_id first, then by vehicle id."""
    if vehicle.trip is not None:
        match = next(
            (
                update
                for update in updates.trip_updates
                if update.trip.trip_id == vehicle.trip.trip_id
            ),
            None,
        )
        if match is not None:
            return match
    return next(
        (
            update
            for update in updates.trip_updates
            if update.vehicle is not None
            and update.vehicle.vehicle_id == vehicle.vehicle.vehicle_id
        ),
        None,
    )


# --- selection -------------------------------------------------------------


def select_vehicle(candidates: tuple[Candidate, ...], strategy: str) -> Candidate | None:
    """Pick one candidate, or None when the strategy finds nothing.

    fence    -> a bus inside the radius; the furthest along if several;
    schedule -> the earliest realtime/scheduled arrival at the stop;
    nearest  -> the smallest straight-line distance to the stop.
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; use one of {STRATEGIES}")
    if not candidates:
        return None

    if strategy == "fence":
        inside = [candidate for candidate in candidates if candidate.in_fence]
        return max(inside, key=_fraction, default=None)
    if strategy == "schedule":
        timed = [candidate for candidate in candidates if candidate.arrival_unix is not None]
        return min(timed, key=lambda candidate: candidate.arrival_unix, default=None)
    nearby = [candidate for candidate in candidates if candidate.distance_m is not None]
    return min(nearby, key=lambda candidate: candidate.distance_m, default=None)


def _fraction(candidate: Candidate) -> float:
    return candidate.progress.fraction if candidate.progress else 0.0


# --- rendering -------------------------------------------------------------


def format_table(report: LineReport | StopReport | VehicleReport) -> str:
    """Plain text for the terminal and for the experiment tests."""
    if isinstance(report, VehicleReport):
        return _format_vehicle(report)
    return _format_candidates(report)


def _format_candidates(report: LineReport | StopReport) -> str:
    heading = f"Line {report.line} (route {report.route_id or '?'})"
    if isinstance(report, StopReport):
        heading += f" — stop {report.stop.stop_id} {report.stop.name}"

    lines = [
        heading,
        _feed_line(report.feed),
        f"strategy: {report.strategy} — * marks the selected vehicle",
        "",
        "  sel  vehicle  trip              next            stops  dist_m  fence  eta    delay   source    stale",
    ]
    for candidate in report.candidates:
        lines.append(_candidate_row(candidate, candidate is report.selected))
    if not report.candidates:
        lines.append("  (no vehicle of this line is currently reporting)")

    return "\n".join(lines)


def _candidate_row(candidate: Candidate, selected: bool) -> str:
    progress = candidate.progress
    next_stop = f"{progress.next_stop_id} (seq {progress.next_stop_sequence})" if progress else "-"
    stops = str(progress.stops_left) if progress else "-"
    distance = f"{candidate.distance_m:.0f}" if candidate.distance_m is not None else "-"
    eta = local_clock(candidate.arrival_unix) if candidate.arrival_unix is not None else "-"
    delay = _delay(candidate.delay_s)
    source = candidate.eta_source or "-"

    return (
        f"  {'*' if selected else ' '}    "
        f"{candidate.vehicle_id:<7}  {candidate.trip_id:<17} {next_stop:<15} "
        f"{stops:>5}  {distance:>6}  {'yes' if candidate.in_fence else 'no':<5}  "
        f"{eta:<5}  {delay:<6}  {source:<8}  {'yes' if candidate.stale else 'no'}"
    )


def _format_vehicle(report: VehicleReport) -> str:
    lines = [
        (
            f"Vehicle {report.vehicle_id} — line {report.line or '?'} "
            f"(route {report.route_id or '?'}), trip {report.trip_id or '?'}"
        ),
        _feed_line(report.feed),
        "",
        "  idx  seq  stop                                    scheduled  expected  delay   source    mark",
    ]
    for row in report.itinerary:
        expected = local_clock(row.expected_unix) if row.expected_unix is not None else "-"
        mark = "*" if row.served else (">" if row.next else "")
        lines.append(
            f"  {row.index:>3}  {row.stop_sequence:>3}  {row.stop_id} {row.name:<33} "
            f"{row.scheduled_time[:5]:<9}  {expected:<8}  {_delay(row.delay_s):<6}  "
            f"{row.source or '-':<8}  {mark}"
        )
    if not report.itinerary:
        lines.append("  (trip not found in the static feed)")
    return "\n".join(lines)


def _feed_line(feed: FeedStatus) -> str:
    calendar = "covered" if feed.calendar_covered else "NOT covered by gtfs.zip"
    fresh = "STALE" if feed.stale else "fresh"
    return (
        f"service day {feed.service_date} ({calendar}); "
        f"positions {feed.vehicle_age_s:.0f}s old, updates {feed.update_age_s:.0f}s old ({fresh})"
    )


def _delay(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    return f"{seconds:+d}s"


# --- small helpers ---------------------------------------------------------


def _now(now: float | None) -> int:
    return int(time.time()) if now is None else int(now)


def _today(now: int) -> str:
    return datetime.fromtimestamp(now, TIMEZONE).strftime("%Y%m%d")


def _is_stale(received_at: float, now: int, stale_after_s: int) -> bool:
    return now - received_at > stale_after_s


def _feed_status(
    feed: StaticFeed,
    vehicles: VehicleSnapshot,
    updates: TripUpdateSnapshot,
    now: int,
    stale_after_s: int,
    service_date: str,
) -> FeedStatus:
    vehicle_age = max(now - vehicles.received_at, 0.0)
    update_age = max(now - updates.received_at, 0.0)
    return FeedStatus(
        service_date=service_date,
        calendar_covered=is_date_covered(feed, service_date),
        vehicle_age_s=vehicle_age,
        update_age_s=update_age,
        stale=vehicle_age > stale_after_s or update_age > stale_after_s,
    )
