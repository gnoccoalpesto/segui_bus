"""Service-day clock: GTFS "HH:MM:SS" -> real instants.

GTFS measures time from "noon minus 12 h" of the service day in the agency's
timezone, so on DST days the origin is not local midnight. Values >= 24:00:00
are legal and mean the next calendar day.

The one safe conversion is: epoch(origin) + seconds. Adding a timedelta to an
aware datetime is wall-clock arithmetic and is wrong twice a year.

See docs/research/2026-10-28-service-day-clock-and-stop-progress.md.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

# The only timezone of this deployment (agency.txt: Europe/Rome).
TIMEZONE = ZoneInfo("Europe/Rome")


def parse_hms(value: str) -> int:
    """ "19:36:00" -> 70560 seconds. Hours may exceed 23 (GTFS allows 25:xx)."""
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def service_day_origin(day: date) -> int:
    """Epoch of the service-day zero: noon minus 12 hours of ``day``."""
    noon = datetime.combine(day, time(12), tzinfo=TIMEZONE)
    return int(noon.timestamp()) - 12 * 3600


def absolute_epoch(day: date, hms: str) -> int:
    """Turn a service-day clock value into a unix timestamp."""
    return service_day_origin(day) + parse_hms(hms)


def parse_service_date(value: str) -> date:
    """ "20261028" -> date(2026, 10, 28): the GTFS Date format."""
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))


def local_clock(epoch: int) -> str:
    """ "19:36" in local time. For display only, never for arithmetic."""
    return datetime.fromtimestamp(epoch, TIMEZONE).strftime("%H:%M")
