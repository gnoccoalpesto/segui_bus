"""Domain objects.

These are plain, frozen, transport-agnostic shapes: the GTFS-RT parsers produce
them, and the lookups / ETA / proximity logic consume them. Nothing here knows
about protobuf, HTTP or the WebSocket.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TripRef:
    """A scheduled run ("corsa"): the thing a vehicle is executing right now."""

    trip_id: str
    route_id: str  # raw value as the feed gives it, e.g. "PC15"
    line: str | None = None  # human line number, e.g. "15"; filled from routes.txt
    direction_id: int | None = None
    start_time: str | None = None  # "19:32:00"
    start_date: str | None = None  # "20261020"


@dataclass(frozen=True, slots=True)
class Vehicle:
    """The bus itself. vehicle_id is the matricola painted on the vehicle."""

    vehicle_id: str
    label: str


@dataclass(frozen=True, slots=True)
class GeoPoint:
    lat: float
    lon: float
    bearing: float | None = None  # degrees, 0=north, 90=east, 180=south, 270=west


@dataclass(frozen=True, slots=True)
class GeoArea:
    """An area on the map, used for proximity. Defined by center and radius.

    Used as a fence: "did the bus enter the area around this point?", which is
    far more robust than comparing exact coordinates.
    """

    point: GeoPoint
    radius: float  # meters


@dataclass(frozen=True, slots=True)
class VehiclePosition:
    """One entry of the vehicle-positions feed."""

    vehicle: Vehicle
    timestamp: int  # unix seconds, when the position was measured
    trip: TripRef | None = None
    position: GeoPoint | None = None
    stop_id: str | None = None
    current_stop_sequence: int | None = None
    current_status: str | None = None  # INCOMING_AT | STOPPED_AT | IN_TRANSIT_TO


@dataclass(frozen=True, slots=True)
class StopTimeUpdate:
    """The expected arrival at one stop of a run (delay is seconds vs schedule)."""

    stop_id: str
    stop_sequence: int | None = None
    arrival_delay_s: int | None = None
    arrival_time: int | None = None  # unix seconds


@dataclass(frozen=True, slots=True)
class TripUpdate:
    """One entry of the trip-updates feed."""

    trip: TripRef
    vehicle: Vehicle | None = None
    stop_time_updates: tuple[StopTimeUpdate, ...] = ()


@dataclass(frozen=True, slots=True)
class VehicleSnapshot:
    """A complete vehicle-positions snapshot (FULL_DATASET: never merged)."""

    feed_timestamp: int  # publication time from the feed header
    received_at: float  # local wall clock (unix seconds) when it was parsed
    vehicles: tuple[VehiclePosition, ...] = ()

    def by_id(self, vehicle_id: str) -> VehiclePosition | None:
        return next((v for v in self.vehicles if v.vehicle.vehicle_id == vehicle_id), None)


@dataclass(frozen=True, slots=True)
class TripUpdateSnapshot:
    """A complete trip-updates snapshot (FULL_DATASET: never merged)."""

    feed_timestamp: int
    received_at: float
    trip_updates: tuple[TripUpdate, ...] = ()


# --- static GTFS (gtfs.zip) ------------------------------------------------
#
# The scheduled world: what exists independently of any running bus. Loaded
# once and kept in memory; the realtime snapshots above are matched against it.


@dataclass(frozen=True, slots=True)
class Stop:
    """A physical boarding point ("fermata")."""

    stop_id: str
    name: str
    point: GeoPoint


@dataclass(frozen=True, slots=True)
class Route:
    """A line ("linea"): the number a rider reads on the front of the bus."""

    route_id: str  # "PC15"
    line: str  # "15" — route_short_name
    long_name: str | None = None
    color: str | None = None


@dataclass(frozen=True, slots=True)
class Trip:
    """A scheduled run of a line, in one direction."""

    trip_id: str
    route_id: str
    direction_id: int | None = None
    headsign: str | None = None
    service_id: str | None = None  # joins calendar_dates.txt: which days it runs


@dataclass(frozen=True, slots=True)
class ScheduledStop:
    """One stop of a trip. arrival_time is a service-day clock: "06:25:00"."""

    stop_id: str
    stop_sequence: int
    arrival_time: str


@dataclass(frozen=True, slots=True)
class StaticFeed:
    """The parsed gtfs.zip, indexed for lookups.

    etag/last_modified identify the exact build this instance came from, so
    cached derived state can be invalidated when the feed is rebuilt.
    """

    etag: str | None
    last_modified: str | None
    stops: Mapping[str, Stop]
    routes: Mapping[str, Route]  # by route_id
    routes_by_line: Mapping[str, Route]  # by human line number, e.g. "15"
    trips: Mapping[str, Trip]  # by trip_id
    trip_stops: Mapping[str, tuple[ScheduledStop, ...]]  # by trip_id, in order
    # Scheduled times keyed by (trip_id, stop_sequence), never by stop_id:
    # loop lines visit the same stop twice in one trip.
    scheduled_by_seq: Mapping[tuple[str, int], ScheduledStop]
    service_dates: Mapping[str, frozenset[str]]  # date "20261028" -> service ids
    calendar_range: tuple[str, str] | None  # first and last covered date

    def scheduled_stop(self, trip_id: str, stop_sequence: int) -> ScheduledStop | None:
        return self.scheduled_by_seq.get((trip_id, stop_sequence))
