"""Domain objects.

These are plain, frozen, transport-agnostic shapes: the GTFS-RT parsers produce
them, and the lookups / ETA / proximity logic consume them. Nothing here knows
about protobuf, HTTP or the WebSocket.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TripRef:
    """A scheduled run ("corsa"): the thing a vehicle is executing right now."""

    trip_id: str
    route_id: str  # raw value as the feed gives it, e.g. "PC15"
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
