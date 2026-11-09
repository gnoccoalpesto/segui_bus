"""The watch engine: one live snapshot in, zero or one alarm out.

The engine composes the existing report and proximity objects. It owns only the
alarm state from issue #2: the global real-alarm latch and the per-trip
preavviso/alarm sent sets. All state is in memory; 0.2.0 has no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..clock import absolute_epoch, parse_service_date
from ..domain import (
    StaticFeed,
    Stop,
    TripUpdate,
    TripUpdateSnapshot,
    VehiclePosition,
    VehicleSnapshot,
)
from ..geo import distance_m
from ..live import LiveData
from ..report import Candidate, FeedStatus, select_vehicle, stop_report

REAL = "real"
PREAVVISO = "preavviso"

TripKey = tuple[str | None, str]


@dataclass(frozen=True, slots=True)
class WatchConfig:
    """The effective values of one followed watch.

    This is the engine's resolved input, not the stored preset: by the time a
    ``WatchConfig`` exists, ``presets.py`` (#28) has already turned every
    ``auto`` sentinel into a concrete value. ``lead_s`` and ``earliest`` are
    required because #3 makes them mandatory on create.
    """

    line: str
    stop: Stop
    lead_s: int
    earliest: str
    stops_n: int
    assumed_speed_kmh: float
    route_detour_factor: float
    position_mismatch_m: float
    strategy: str
    radius_m: float = 150.0
    on_time_tolerance_s: int = 240
    stale_after_s: int = 120


@dataclass(frozen=True, slots=True)
class AlarmEvent:
    """What to notify, about which candidate, and why."""

    kind: str
    line: str
    headsign: str | None
    vehicle_id: str
    trip_id: str
    service_date: str | None
    route_id: str | None
    direction_id: int | None
    stop_name: str
    stops_to_target: int | None
    distance_m: float | None
    seconds_from_now: int | None
    arrival_unix: int | None
    delay_s: int | None
    eta_source: str | None
    on_time_tolerance_s: int
    stale: bool
    stale_age_s: int
    snapshot_unix: int
    malformed: bool
    arms: tuple[str, ...]


@dataclass(slots=True)
class WatchEngine:
    """Stateful evaluator for a single followed watch."""

    watch: WatchConfig
    alerted_trip: TripKey | None = None
    preavviso_sent: set[TripKey] = field(default_factory=set)
    alarm_sent: set[TripKey] = field(default_factory=set)

    def tick(self, live: LiveData) -> tuple[AlarmEvent, ...]:
        report = stop_report(
            live.feed,
            live.vehicles,
            live.updates,
            self.watch.line,
            self.watch.stop,
            radius_m=self.watch.radius_m,
            now=live.now,
            stale_after_s=self.watch.stale_after_s,
            strategy=self.watch.strategy,
        )
        candidates = report.candidates
        malformed = {
            candidate.vehicle_id: _is_malformed(
                live.feed,
                live.vehicles.by_id(candidate.vehicle_id),
                _update_for(live.updates, candidate.vehicle_id),
                position_mismatch_m=self.watch.position_mismatch_m,
            )
            for candidate in candidates
        }

        self._release(candidates, malformed)

        qualified = (
            (candidate, self._arms(candidate, live, malformed[candidate.vehicle_id]))
            for candidate in candidates
        )
        qualified = [(candidate, arms) for candidate, arms in qualified if arms]

        real = [
            (candidate, arms)
            for candidate, arms in qualified
            if any(is_real for _, is_real in arms)
        ]
        preavviso = [
            (candidate, arms)
            for candidate, arms in qualified
            if not any(is_real for _, is_real in arms)
        ]

        if real:
            return self._emit(real, live, report.feed, malformed, REAL)
        if preavviso:
            return self._emit(preavviso, live, report.feed, malformed, PREAVVISO)
        return ()

    def reset(self) -> None:
        """Discard all alarm state, as when following another watch."""
        self.alerted_trip = None
        self.preavviso_sent.clear()
        self.alarm_sent.clear()

    def _release(
        self,
        candidates: tuple[Candidate, ...],
        malformed: dict[str, bool],
    ) -> None:
        if self.alerted_trip is None:
            return
        for candidate in candidates:
            if _key(candidate) != self.alerted_trip:
                continue
            if malformed[candidate.vehicle_id]:
                return
            if candidate.serves_stop and candidate.stops_to_target is not None:
                return
        self.alerted_trip = None

    def _arms(
        self,
        candidate: Candidate,
        live: LiveData,
        malformed: bool,
    ) -> tuple[tuple[str, bool], ...]:
        if not self._after_earliest(candidate, live):
            return ()

        if malformed:
            if candidate.in_fence:
                return (("fence", True),)
            return ()

        if not candidate.serves_stop or candidate.stops_to_target is None:
            return ()

        arms: list[tuple[str, bool]] = []
        if candidate.in_fence:
            arms.append(("fence", True))
        if candidate.stops_to_target <= self.watch.stops_n:
            arms.append(("stops", True))

        remaining = _remaining_seconds(candidate, live, self.watch)
        if remaining is not None and remaining <= self.watch.lead_s:
            arms.append(("eta", self._on_time(candidate)))

        return tuple(arms)

    def _after_earliest(self, candidate: Candidate, live: LiveData) -> bool:
        if candidate.service_date is None:
            return True
        arrival = candidate.arrival_unix if candidate.arrival_unix is not None else live.now
        earliest = absolute_epoch(parse_service_date(candidate.service_date), self.watch.earliest)
        return arrival >= earliest

    def _on_time(self, candidate: Candidate) -> bool:
        return (
            candidate.eta_source == "realtime"
            and candidate.delay_s is not None
            and abs(candidate.delay_s) <= self.watch.on_time_tolerance_s
        )

    def _emit(
        self,
        qualifying: list[tuple[Candidate, tuple[tuple[str, bool], ...]]],
        live: LiveData,
        feed_status: FeedStatus,
        malformed: dict[str, bool],
        kind: str,
    ) -> tuple[AlarmEvent, ...]:
        winner = select_vehicle(
            tuple(candidate for candidate, _ in qualifying),
            self.watch.strategy,
        )
        if winner is None:
            return ()

        arms = next(
            arms for candidate, arms in qualifying if candidate.vehicle_id == winner.vehicle_id
        )
        key = _key(winner)

        if kind == REAL:
            if self.alerted_trip is not None or key in self.alarm_sent:
                return ()
            self.alarm_sent.add(key)
            self.alerted_trip = key
        else:
            if key in self.preavviso_sent or key in self.alarm_sent:
                return ()
            self.preavviso_sent.add(key)

        return (
            _event(
                winner,
                arms,
                live,
                feed_status,
                malformed[winner.vehicle_id],
                kind,
                self.watch,
            ),
        )


def _event(
    candidate: Candidate,
    arms: tuple[tuple[str, bool], ...],
    live: LiveData,
    feed_status: FeedStatus,
    malformed: bool,
    kind: str,
    watch: WatchConfig,
) -> AlarmEvent:
    remaining = None if malformed else _remaining_seconds(candidate, live, watch)
    return AlarmEvent(
        kind=kind,
        line=candidate.line or watch.line,
        headsign=candidate.headsign,
        vehicle_id=candidate.vehicle_id,
        trip_id=candidate.trip_id,
        service_date=candidate.service_date,
        route_id=candidate.route_id,
        direction_id=candidate.direction_id,
        stop_name=watch.stop.name,
        stops_to_target=None if malformed else candidate.stops_to_target,
        distance_m=candidate.distance_m,
        seconds_from_now=remaining,
        arrival_unix=None if remaining is None else live.now + remaining,
        delay_s=None if malformed else candidate.delay_s,
        eta_source=None if malformed else candidate.eta_source,
        on_time_tolerance_s=watch.on_time_tolerance_s,
        stale=candidate.stale,
        stale_age_s=int(max(feed_status.vehicle_age_s, feed_status.update_age_s)),
        snapshot_unix=_snapshot_at(live.vehicles),
        malformed=malformed,
        arms=tuple(name for name, _ in arms),
    )


def _remaining_seconds(
    candidate: Candidate,
    live: LiveData,
    watch: WatchConfig,
) -> int | None:
    if not candidate.stale:
        if candidate.arrival_unix is None:
            return None
        return candidate.arrival_unix - live.now

    projections = []
    if candidate.arrival_unix is not None:
        projections.append(candidate.arrival_unix - live.now)

    schedule = _schedule_remainder(candidate, live)
    if schedule is not None:
        projections.append(schedule)

    position = _position_projection(candidate, watch)
    if position is not None:
        projections.append(position)

    if not projections:
        return None
    return round(max(projections))


def _schedule_remainder(candidate: Candidate, live: LiveData) -> float | None:
    if candidate.scheduled_unix is None or candidate.progress is None:
        return None
    if candidate.service_date is None:
        return None

    anchor_sequence = candidate.progress.next_stop_sequence
    if anchor_sequence is None and candidate.progress.served_index is not None:
        stops = live.feed.trip_stops.get(candidate.trip_id, ())
        if 0 <= candidate.progress.served_index < len(stops):
            anchor_sequence = stops[candidate.progress.served_index].stop_sequence
    if anchor_sequence is None:
        return None

    anchor = live.feed.scheduled_stop(candidate.trip_id, anchor_sequence)
    if anchor is None:
        return None

    anchor_unix = absolute_epoch(
        parse_service_date(candidate.service_date),
        anchor.arrival_time,
    )
    delay = candidate.delay_s if candidate.delay_s is not None else 0
    snapshot_at = _snapshot_at(live.vehicles)
    return (candidate.scheduled_unix - anchor_unix) + delay - (live.now - snapshot_at)


def _position_projection(candidate: Candidate, watch: WatchConfig) -> float | None:
    if candidate.distance_m is None:
        return None
    speed_mps = watch.assumed_speed_kmh / 3.6
    if speed_mps <= 0:
        return None
    return watch.route_detour_factor * candidate.distance_m / speed_mps


def _key(candidate: Candidate) -> TripKey:
    return (candidate.service_date, candidate.trip_id)


def _update_for(updates: TripUpdateSnapshot, vehicle_id: str) -> TripUpdate | None:
    return next(
        (
            update
            for update in updates.trip_updates
            if update.vehicle is not None and update.vehicle.vehicle_id == vehicle_id
        ),
        None,
    )


def _is_malformed(
    feed: StaticFeed,
    vehicle: VehiclePosition | None,
    update: TripUpdate | None,
    *,
    position_mismatch_m: float,
) -> bool:
    if vehicle is None:
        return False
    if vehicle.trip is None:
        return True

    trip = vehicle.trip
    if trip.trip_id not in feed.trips:
        return True

    stops = feed.trip_stops.get(trip.trip_id, ())
    sequence = vehicle.current_stop_sequence
    claimed = next(
        (scheduled for scheduled in stops if scheduled.stop_sequence == sequence),
        None,
    )
    if claimed is None:
        return True

    if update is not None:
        if update.trip.trip_id != trip.trip_id:
            return True
        if (
            trip.start_date is not None
            and update.trip.start_date is not None
            and trip.start_date != update.trip.start_date
        ):
            return True

    if vehicle.stop_id is not None and claimed.stop_id != vehicle.stop_id:
        return True

    if vehicle.position is not None:
        stop = feed.stops.get(claimed.stop_id)
        if stop is not None and distance_m(vehicle.position, stop.point) > position_mismatch_m:
            return True

    return False


def _snapshot_at(vehicles: VehicleSnapshot) -> int:
    return vehicles.feed_timestamp if vehicles.feed_timestamp > 0 else int(vehicles.received_at)
