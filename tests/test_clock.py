"""Service-day clock tests.

The DST rows come from the research note's table: the origin is noon-minus-12h,
which is midnight only on ordinary days.
"""

from datetime import date, datetime

import pytest

from segui_bus.clock import (
    TIMEZONE,
    absolute_epoch,
    local_clock,
    parse_hms,
    parse_service_date,
    service_day_origin,
)


def _local(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, TIMEZONE)


def test_parse_hms_accepts_hours_past_midnight():
    assert parse_hms("19:36:00") == 19 * 3600 + 36 * 60
    assert parse_hms("25:10:00") == 25 * 3600 + 10 * 60  # legal in GTFS


def test_ordinary_day_starts_at_local_midnight():
    origin = service_day_origin(date(2026, 10, 28))

    assert _local(origin).isoformat() == "2026-10-28T00:00:00+01:00"


def test_fall_back_day_starts_at_one_am():
    # 2026-10-25: noon minus 12h is 01:00 CEST, not 00:00.
    origin = service_day_origin(date(2026, 10, 25))

    assert _local(origin).isoformat() == "2026-10-25T01:00:00+02:00"


def test_spring_forward_day_starts_on_the_previous_evening():
    # 2027-03-28: noon minus 12h is 23:00 CET on the 27th.
    origin = service_day_origin(date(2027, 3, 28))

    assert _local(origin).isoformat() == "2027-03-27T23:00:00+01:00"


def test_morning_trip_keeps_its_wall_clock_on_a_dst_day():
    # 05:00 stays 05:00 local on both sides of the transition: the origin absorbs it.
    assert _local(absolute_epoch(date(2026, 10, 24), "05:00:00")).strftime("%H:%M") == "05:00"
    assert _local(absolute_epoch(date(2026, 10, 25), "05:00:00")).strftime("%H:%M") == "05:00"


def test_after_midnight_times_land_on_the_next_day():
    epoch = absolute_epoch(date(2026, 10, 28), "25:10:00")

    assert _local(epoch).isoformat() == "2026-10-29T01:10:00+01:00"


def test_parse_service_date():
    assert parse_service_date("20261028") == date(2026, 10, 28)


def test_local_clock_is_for_display():
    assert local_clock(absolute_epoch(date(2026, 10, 28), "19:36:00")) == "19:36"


@pytest.mark.parametrize("hms", ["00:00:00", "12:54:00", "22:53:00"])
def test_round_trip_through_the_epoch(hms: str):
    epoch = absolute_epoch(date(2026, 10, 28), hms)
    assert local_clock(epoch) == hms[:5]
