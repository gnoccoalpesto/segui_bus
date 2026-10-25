"""GTFS-RT protobuf -> domain objects.

Two rules from the research note (§4.3) are enforced here:

* FULL_DATASET: a snapshot is complete, so it replaces the previous one;
* dedupe on vehicle id: the same bus can appear twice in one emission.

The parser is pure: bytes in, snapshot out. It never fetches anything.
"""

from __future__ import annotations

import time

from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2 as pb

from .domain import (
    GeoPoint,
    StopTimeUpdate,
    TripRef,
    TripUpdate,
    TripUpdateSnapshot,
    Vehicle,
    VehiclePosition,
    VehicleSnapshot,
)

# --- small field converters ------------------------------------------------


def _parse_feed(payload: bytes) -> pb.FeedMessage:
    """Decode the protobuf envelope, turning a bad payload into a clear error."""
    feed = pb.FeedMessage()
    try:
        feed.ParseFromString(payload)
    except DecodeError as exc:
        raise ValueError("payload is not a GTFS-RT FeedMessage") from exc
    return feed


def _optional(container, field: str):
    """Read an optional proto2 field, or None when it was not sent.

    GTFS-RT is proto2: "absent" is meaningful (e.g. speed, bearing, delay), so
    HasField is used instead of the default zero value.
    """
    return getattr(container, field) if container.HasField(field) else None


def _trip_ref(trip: pb.TripDescriptor) -> TripRef:
    """TripDescriptor -> TripRef. Optional proto fields become None."""
    return TripRef(
        trip_id=trip.trip_id,
        route_id=trip.route_id,
        direction_id=_optional(trip, "direction_id"),
        start_time=trip.start_time or None,
        start_date=trip.start_date or None,
    )


def _vehicle(vehicle: pb.VehicleDescriptor) -> Vehicle:
    return Vehicle(
        vehicle_id=vehicle.id,
        label=vehicle.label or vehicle.id,
    )


def _status_name(value: int) -> str | None:
    """Enum int -> readable name, so the domain never stores raw protobuf enums."""
    try:
        return pb.VehiclePosition.VehicleStopStatus.Name(value)
    except ValueError:
        return None


def _geo_point(vehicle: pb.VehiclePosition) -> GeoPoint | None:
    if not vehicle.HasField("position"):
        return None
    pos = vehicle.position
    return GeoPoint(
        lat=pos.latitude,
        lon=pos.longitude,
        bearing=_optional(pos, "bearing"),
    )


# --- vehicle positions -----------------------------------------------------


def _parse_vehicle_position(entity: pb.FeedEntity) -> VehiclePosition | None:
    """One FeedEntity -> VehiclePosition, or None if it carries no vehicle."""
    if not entity.HasField("vehicle"):
        return None
    v = entity.vehicle
    return VehiclePosition(
        vehicle=_vehicle(v.vehicle),
        timestamp=v.timestamp,
        trip=_trip_ref(v.trip) if v.HasField("trip") else None,
        position=_geo_point(v),
        stop_id=v.stop_id or None,
        current_stop_sequence=_optional(v, "current_stop_sequence"),
        current_status=_status_name(v.current_status),
    )


def _dedupe_vehicles(vehicles: list[VehiclePosition]) -> tuple[VehiclePosition, ...]:
    """Keep one entry per vehicle id: the freshest one wins.

    Byte-identical duplicates exist in this feed (research note §6), and a
    re-emission can carry a newer timestamp for the same bus.
    """
    freshest: dict[str, VehiclePosition] = {}
    for vp in vehicles:
        current = freshest.get(vp.vehicle.vehicle_id)
        if current is None or vp.timestamp > current.timestamp:
            freshest[vp.vehicle.vehicle_id] = vp
    # Sorted by id so output order is stable and testable.
    return tuple(sorted(freshest.values(), key=lambda vp: vp.vehicle.vehicle_id))


def parse_vehicle_positions(payload: bytes, received_at: float | None = None) -> VehicleSnapshot:
    feed = _parse_feed(payload)

    positions = [
        parsed for entity in feed.entity if (parsed := _parse_vehicle_position(entity)) is not None
    ]
    return VehicleSnapshot(
        feed_timestamp=feed.header.timestamp,
        received_at=received_at if received_at is not None else time.time(),
        vehicles=_dedupe_vehicles(positions),
    )


# --- trip updates ----------------------------------------------------------


def _stop_time_update(stu: pb.TripUpdate.StopTimeUpdate) -> StopTimeUpdate:
    """One StopTimeUpdate; arrival may be absent on pass-throughs."""
    arrival = stu.arrival if stu.HasField("arrival") else None
    return StopTimeUpdate(
        stop_id=stu.stop_id,
        stop_sequence=_optional(stu, "stop_sequence"),
        arrival_delay_s=_optional(arrival, "delay") if arrival is not None else None,
        arrival_time=_optional(arrival, "time") if arrival is not None else None,
    )


def _parse_trip_update(entity: pb.FeedEntity) -> TripUpdate | None:
    if not entity.HasField("trip_update"):
        return None
    tu = entity.trip_update
    return TripUpdate(
        trip=_trip_ref(tu.trip),
        vehicle=_vehicle(tu.vehicle) if tu.HasField("vehicle") else None,
        stop_time_updates=tuple(_stop_time_update(stu) for stu in tu.stop_time_update),
    )


def _dedupe_trips(trip_updates: list[TripUpdate]) -> tuple[TripUpdate, ...]:
    """One entry per trip id, first occurrence wins (feed order is arbitrary)."""
    seen: dict[str, TripUpdate] = {}
    for tu in trip_updates:
        seen.setdefault(tu.trip.trip_id, tu)
    return tuple(sorted(seen.values(), key=lambda tu: tu.trip.trip_id))


def parse_trip_updates(payload: bytes, received_at: float | None = None) -> TripUpdateSnapshot:
    feed = _parse_feed(payload)

    updates = [
        parsed for entity in feed.entity if (parsed := _parse_trip_update(entity)) is not None
    ]
    return TripUpdateSnapshot(
        feed_timestamp=feed.header.timestamp,
        received_at=received_at if received_at is not None else time.time(),
        trip_updates=_dedupe_trips(updates),
    )
