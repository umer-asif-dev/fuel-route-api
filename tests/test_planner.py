"""Planner edge cases using a hand-built station index (no database)."""

import pytest

from routing.clients.ors import RouteResult
from routing.exceptions import NoFuelPlan
from routing.services.planner import RoutePlanner
from stations.services.index import StationIndex, StationInfo

# Routes are stored in the database, so these tests need it (but not the station data).
pytestmark = pytest.mark.django_db

# A straight east-west route along 40°N, about 1,060 miles long.
ROUTE = RouteResult(
    coordinates=[[-100.0 + i * 0.1, 40.0] for i in range(201)],
    distance_miles=1060.0,
    duration_hours=16.0,
    profile="driving-hgv",
)


class StubClient:
    calls_made = 0

    def directions(self, start, finish, profile):
        self.calls_made += 1
        return ROUTE


def station(opis_id, lon, price, lat=40.0):
    return StationInfo(opis_id, f"S{opis_id}", "", "Town", "KS", price, lat, lon)


def planner(*stations):
    return RoutePlanner(station_index=StationIndex.from_infos(list(stations)), client=StubClient())


def test_plan_uses_stations_near_the_route():
    body = planner(
        station(1, -99.95, 3.5),
        station(2, -94.0, 3.0),
        station(3, -89.0, 3.2),
        station(4, -94.0, 1.0, lat=41.5),  # cheap but ~100 miles off route
    ).plan("40,-100", "40,-80")
    ids = [s["station"]["opis_id"] for s in body["fuel"]["stops"]]
    assert ids[0] == 1 and 4 not in ids
    assert body["fuel"]["total_gallons"] == pytest.approx(106, abs=0.05)


def test_no_station_near_start_is_reported():
    with pytest.raises(NoFuelPlan, match="within 25 miles of the start"):
        planner(station(1, -95.0, 3.0)).plan("40,-100", "40,-80")


def test_gap_longer_than_range_is_reported():
    with pytest.raises(NoFuelPlan, match="No fuel station between mile"):
        planner(station(1, -99.95, 3.0), station(2, -90.0, 3.0)).plan("40,-100", "40,-80")


def test_corridor_widens_automatically_when_needed():
    # The only mid-route station is ~20 miles off the road.
    body = planner(station(1, -99.95, 3.0), station(2, -93.0, 3.0, lat=40.29), station(3, -87.0, 3.0)).plan(
        "40,-100", "40,-80"
    )
    assert body["assumptions"]["corridor_miles_requested"] == 10
    assert body["assumptions"]["corridor_miles_used"] == 25
