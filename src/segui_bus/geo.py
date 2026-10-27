"""Pure geometry: distances and the area fence.

No IO and no configuration. Everything is expressed in metres, so no other
module has to think about units.
"""

from __future__ import annotations

import math

from .domain import GeoArea, GeoPoint

EARTH_RADIUS_M = 6_371_008.8  # mean Earth radius


def distance_m(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance between two points, in metres (haversine)."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlon = math.radians(b.lon - a.lon)

    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def contains(area: GeoArea, point: GeoPoint) -> bool:
    """The fence: is the point inside the area? Inclusive at the boundary.

    This is the robust replacement for comparing rounded coordinates: instead
    of asking "is the bus exactly here?" we ask "has it entered this area?".
    """
    return distance_m(area.point, point) <= area.radius


def around(point: GeoPoint, radius: float) -> GeoArea:
    """Build an area centred on a point."""
    return GeoArea(point=point, radius=radius)
