"""End-to-end trips from the plan, on real road geometry and the real station data."""

import math

import pytest
from django.urls import reverse

from stations.services.index import StationIndex, get_station_index

pytestmark = pytest.mark.django_db

TRIPS = {
    "short (under one tank)": ("Dallas, TX", "Austin, TX", 196.0),
    "Chicago to Denver": ("Chicago, IL", "Denver, CO", 1005.1),
    "Chicago to Houston": ("Chicago, IL", "Houston, TX", 1083.1),
    "New York to Los Angeles": ("New York, NY", "Los Angeles, CA", 2810.4),
    "Seattle to Miami": ("Seattle, WA", "Miami, FL", 3303.0),
}


def plan(client, start, finish, **extra):
    response = client.get(reverse("route"), {"start": start, "finish": finish, **extra})
    assert response.status_code == 200, response.json()
    return response.json()


def check_plan_is_valid(body):
    """Physical sanity checks that must hold for any plan."""
    distance = body["route"]["distance_miles"]
    fuel = body["fuel"]
    stops = fuel["stops"]
    corridor = body["assumptions"]["corridor_miles_used"]
    prices = get_station_index().price

    # Starting empty, every mile is bought: gallons = miles / 10.
    assert fuel["total_gallons"] == pytest.approx(distance / 10, abs=0.05)
    assert fuel["total_cost"] == pytest.approx(sum(s["cost"] for s in stops), abs=0.05)
    assert prices.min() * fuel["total_gallons"] <= fuel["total_cost"] <= prices.max() * fuel["total_gallons"]
    assert fuel["stop_count"] >= math.ceil(distance / 500)

    # The first fill-up is near the origin; stops are in route order.
    assert stops[0]["mile_marker"] <= 25 and stops[0]["fuel_on_arrival_gallons"] == 0
    assert [s["order"] for s in stops] == list(range(1, len(stops) + 1))
    markers = [s["mile_marker"] for s in stops]
    assert markers == sorted(markers)

    # Never over a full tank; never more than one tank between stops or to the finish.
    for stop in stops:
        assert stop["fuel_on_arrival_gallons"] >= 0
        assert stop["fuel_on_arrival_gallons"] + stop["gallons"] <= 50.01
        assert stop["off_route_miles"] <= corridor
        assert stop["cost"] == pytest.approx(stop["gallons"] * stop["price_per_gallon"], abs=0.03)
    for a, b in zip(stops, stops[1:], strict=False):
        assert b["mile_marker"] - a["mile_marker"] <= 500 + 25
    assert distance - stops[-1]["mile_marker"] <= 500 + 0.5


@pytest.mark.parametrize("trip", TRIPS, ids=list(TRIPS))
def test_trip_produces_a_valid_cheap_plan(client, fake_ors, trip):
    start, finish, miles = TRIPS[trip]
    body = plan(client, start, finish)

    assert body["route"]["distance_miles"] == pytest.approx(miles, abs=0.1)
    assert body["meta"]["external_calls"] == 1
    check_plan_is_valid(body)

    # The plan is never worse than filling up at the first stop and buying everything there.
    first_price = body["fuel"]["stops"][0]["price_per_gallon"]
    if miles <= 500:
        assert body["fuel"]["stop_count"] == 1
        assert body["fuel"]["total_cost"] == pytest.approx(first_price * miles / 10, abs=0.05)

    page = client.get(reverse("route-map"), {"start": start, "finish": finish})
    assert page.status_code == 200
    assert len(fake_ors.calls) == 1


def test_optimised_price_beats_the_corridor_average(client, fake_ors):
    """The plan should pay well under what a typical station along the route charges."""
    from routing.services.route_geometry import RouteLine, match_stations_to_route

    for start, finish, _ in TRIPS.values():
        body = plan(client, start, finish)
        index = get_station_index()
        line = RouteLine.from_lonlat(body["geojson"]["features"][0]["geometry"]["coordinates"])
        corridor = match_stations_to_route(line, index.lat, index.lon, 10)
        corridor_avg = float(index.price[corridor.index].mean())
        assert body["fuel"]["average_price_per_gallon"] < corridor_avg, (start, finish)


def test_wider_corridor_never_costs_more(client, fake_ors):
    narrow = plan(client, "Chicago, IL", "Houston, TX", corridor_miles=5)
    normal = plan(client, "Chicago, IL", "Houston, TX")
    wide = plan(client, "Chicago, IL", "Houston, TX", corridor_miles=40)
    for body in (narrow, normal, wide):
        check_plan_is_valid(body)
    # More candidate stations can only lower the optimum (small-stop merging adds cents).
    assert wide["fuel"]["total_cost"] <= normal["fuel"]["total_cost"] + 0.5
    assert normal["fuel"]["total_cost"] <= narrow["fuel"]["total_cost"] + 0.5
    assert (
        wide["meta"]["stations_in_corridor"]
        > normal["meta"]["stations_in_corridor"]
        > narrow["meta"]["stations_in_corridor"]
    )
    assert len(fake_ors.calls) == 1


def _index_without(states):
    full = get_station_index()
    return StationIndex.from_infos([s for s in full.stations if s.state not in states])


def test_long_station_gap_is_422_with_location(client, fake_ors, monkeypatch):
    # The route runs I-70/I-15; without Utah, Nevada or Arizona stations there is no fuel
    # between western Colorado and southern California.
    sparse = _index_without({"UT", "NV", "AZ"})
    monkeypatch.setattr("routing.services.planner.get_station_index", lambda: sparse)
    response = client.get(reverse("route"), {"start": "New York, NY", "finish": "Los Angeles, CA"})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "no_fuel_plan"
    assert error["details"]["gap_to_mile"] - error["details"]["gap_from_mile"] > 500


def test_no_station_near_origin_is_422(client, fake_ors, monkeypatch):
    sparse = _index_without({"WA"})
    monkeypatch.setattr("routing.services.planner.get_station_index", lambda: sparse)
    response = client.get(reverse("route"), {"start": "Seattle, WA", "finish": "Miami, FL"})
    assert response.status_code == 422
    assert "of the start" in response.json()["error"]["message"]


def test_sparse_stretch_widens_corridor_automatically(client, fake_ors, monkeypatch):
    """Remove every station within 12 miles of I-80 in Nebraska; only towns further off remain."""
    from routing.services.route_geometry import RouteLine, match_stations_to_route

    full = get_station_index()
    body = plan(client, "Chicago, IL", "Denver, CO")
    line = RouteLine.from_lonlat(body["geojson"]["features"][0]["geometry"]["coordinates"])
    near = set(match_stations_to_route(line, full.lat, full.lon, 12).index.tolist())
    sparse = StationIndex.from_infos(
        [s for i, s in enumerate(full.stations) if not (s.state in {"NE", "CO"} and i in near)]
    )
    monkeypatch.setattr("routing.services.planner.get_station_index", lambda: sparse)

    widened = plan(client, "Chicago, IL", "Denver, CO")
    assert widened["assumptions"]["corridor_miles_requested"] == 10
    assert widened["assumptions"]["corridor_miles_used"] in (25, 50)
    check_plan_is_valid(widened)
    assert any(s["off_route_miles"] > 10 for s in widened["fuel"]["stops"])
