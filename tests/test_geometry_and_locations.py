import numpy as np
import pytest

from routing.services.locations import in_usa, parse_city_state, resolve_location
from routing.services.route_geometry import RouteLine, cumulative_miles, match_stations_to_route, simplify


def no_geocoder(text):
    raise AssertionError(f"geocoder should not be called for {text!r}")


def test_one_degree_of_latitude_is_about_69_miles():
    miles = cumulative_miles(np.array([40.0, 41.0]), np.array([-100.0, -100.0]))
    assert miles[-1] == pytest.approx(69.09, abs=0.05)


def test_route_is_densified_and_scaled():
    from routing.services.route_geometry import haversine_miles

    line = RouteLine.from_lonlat([[-100, 40], [-99, 40], [-99, 40.5]], spacing_miles=0.5, scale_to_miles=100)
    steps = haversine_miles(line.lat[:-1], line.lon[:-1], line.lat[1:], line.lon[1:])
    assert steps.max() <= 0.5 + 1e-9  # no gap wider than the spacing
    assert line.length_miles == pytest.approx(100)  # markers agree with the routing engine's distance
    assert (line.lat[0], line.lon[0]) == (40, -100) and (line.lat[-1], line.lon[-1]) == (40.5, -99)


def test_match_stations_to_route_reports_mile_and_offset():
    line = RouteLine.from_lonlat([[-100, 40], [-98, 40]])
    lat = np.array([40.0, 40.1, 40.5, 40.0])
    lon = np.array([-99.0, -98.5, -99.0, -90.0])
    m = match_stations_to_route(line, lat, lon, corridor_miles=10)
    assert sorted(m.index.tolist()) == [0, 1]
    first = m.index.tolist().index(0)
    assert m.mile[first] == pytest.approx(line.length_miles / 2, abs=0.5)
    assert m.off_route_miles[first] == pytest.approx(0, abs=0.3)


def test_simplify_keeps_endpoints_and_drops_collinear_points():
    lat = np.full(100, 40.0)
    lon = np.linspace(-100, -90, 100)
    keep = simplify(lat, lon, 0.01)
    assert keep.tolist() == [0, 99]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Chicago, IL", ("Chicago", "IL")),
        ("chicago il", ("chicago", "IL")),
        ("Kansas City, Missouri", ("Kansas City", "MO")),
        ("New York, NY, USA", ("New York", "NY")),
        ("Saint Louis, MO.", ("Saint Louis", "MO")),
        ("Toronto, ON", None),
        ("1600 Pennsylvania Ave", None),
    ],
)
def test_parse_city_state(text, expected):
    assert parse_city_state(text) == expected


def test_resolve_city_and_coordinates_without_api_calls():
    chicago = resolve_location("Chicago, IL", no_geocoder)
    assert chicago.source == "gazetteer" and 41.5 < chicago.lat < 42.2
    point = resolve_location(" 39.7392, -104.9903 ", no_geocoder)
    assert point.source == "coordinates" and point.lon == -104.9903


def test_in_usa():
    assert in_usa(41.88, -87.63)  # Chicago
    assert in_usa(61.2, -149.9)  # Anchorage
    assert in_usa(21.3, -157.8)  # Honolulu
    assert in_usa(24.555, -81.78)  # Key West
    assert not in_usa(49.28, -123.12)  # Vancouver
    assert not in_usa(32.51, -117.04)  # Tijuana
    assert not in_usa(43.65, -79.38)  # Toronto
    assert not in_usa(19.43, -99.13)  # Mexico City


@pytest.mark.parametrize(
    "text", ["Toronto, ON", "Vancouver, BC", "Montreal, Quebec", "Calgary, Canada", "Monterrey, Mexico"]
)
def test_foreign_regions_are_rejected_without_geocoding(text):
    from routing.exceptions import LocationOutsideUSA

    with pytest.raises(LocationOutsideUSA):
        resolve_location(text, no_geocoder)


@pytest.mark.parametrize("text", ["Santa Fe, New Mexico", "Albuquerque, NM", "Mexico, MO", "Canada, KY"])
def test_us_places_with_foreign_sounding_names_are_accepted(text):
    # Canada, KY isn't in the Census list, so it falls through to the geocoder.
    location = resolve_location(text, lambda q: (37.6, -82.3, "Canada, KY"))
    assert location.source in {"gazetteer", "geocoder"}
