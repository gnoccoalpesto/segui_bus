"""Geometry tests: distances in metres and the area fence."""

import pytest

from segui_bus.domain import GeoPoint
from segui_bus.geo import around, contains, distance_m

PIACENZA = GeoPoint(lat=45.0521, lon=9.7053)


def test_distance_to_itself_is_zero():
    assert distance_m(PIACENZA, PIACENZA) == pytest.approx(0.0)


def test_one_thousandth_of_a_degree_of_latitude_is_about_111_m():
    north = GeoPoint(lat=PIACENZA.lat + 0.001, lon=PIACENZA.lon)

    assert distance_m(PIACENZA, north) == pytest.approx(111.2, abs=1.0)


def test_longitude_shrinks_with_latitude():
    east = GeoPoint(lat=PIACENZA.lat, lon=PIACENZA.lon + 0.001)

    # 111.3 m * cos(45 deg) ~= 78.7 m: a degree of longitude is shorter up north.
    assert distance_m(PIACENZA, east) == pytest.approx(78.7, abs=1.0)


def test_contains_is_inclusive_at_the_boundary():
    target = GeoPoint(lat=45.0531, lon=9.7053)
    exact_radius = distance_m(PIACENZA, target)

    assert contains(around(PIACENZA, exact_radius), target)
    assert not contains(around(PIACENZA, exact_radius - 0.01), target)


def test_contains_rejects_a_point_outside_the_area():
    far_away = GeoPoint(lat=45.1000, lon=9.8000)

    assert not contains(around(PIACENZA, 150.0), far_away)
