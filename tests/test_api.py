"""API contract, caching and every error path, against the simulated OpenRouteService."""

import pytest
import requests
from django.urls import reverse

from tests.conftest import FakeResponse, ors_error

pytestmark = pytest.mark.django_db

CHI_DEN = {"start": "Chicago, IL", "finish": "Denver, CO"}


def get_route(client, **params):
    return client.get(reverse("route"), params)


def error_code(response):
    return response.json()["error"]["code"]


# --- happy path and response contract ------------------------------------


def test_response_contract(client, fake_ors):
    response = get_route(client, **CHI_DEN)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"start", "finish", "route", "fuel", "assumptions", "geojson", "meta", "map_url"}
    assert set(body["fuel"]) == {"total_cost", "total_gallons", "average_price_per_gallon", "stop_count", "stops"}
    stop = body["fuel"]["stops"][0]
    assert set(stop) == {
        "order",
        "station",
        "mile_marker",
        "off_route_miles",
        "price_per_gallon",
        "gallons",
        "cost",
        "fuel_on_arrival_gallons",
    }
    assert set(stop["station"]) == {"opis_id", "name", "address", "city", "state", "lat", "lon"}
    assert body["assumptions"] == {
        "vehicle_range_miles": 500,
        "mpg": 10,
        "starts_with_empty_tank": True,
        "corridor_miles_requested": 10,
        "corridor_miles_used": 10,
    }
    assert body["geojson"]["type"] == "FeatureCollection"
    assert body["map_url"] == "http://testserver/api/v1/route/map/?start=Chicago%2C+IL&finish=Denver%2C+CO"


def test_single_routing_call_with_key_profile_and_lon_lat_order(client, fake_ors):
    body = get_route(client, **CHI_DEN).json()
    assert body["meta"]["external_calls"] == 1
    (call,) = fake_ors.calls
    assert call["method"] == "POST"
    assert call["url"] == "https://api.openrouteservice.org/v2/directions/driving-hgv/geojson"
    assert call["json"]["coordinates"] == [[-87.684939, 41.837045], [-104.881105, 39.76185]]
    assert call["json"]["instructions"] is False
    assert call["timeout"] == 20


def test_post_json_body_gives_same_result(client, fake_ors):
    posted = client.post(reverse("route"), CHI_DEN, content_type="application/json").json()
    fetched = get_route(client, **CHI_DEN).json()
    assert posted["fuel"] == fetched["fuel"]


def test_browsable_api_renders_for_browsers(client, fake_ors):
    response = client.get(reverse("route"), CHI_DEN, HTTP_ACCEPT="text/html")
    assert response.status_code == 200 and b"Route" in response.content


# --- stored results ---------------------------------------------------------------


def test_repeat_request_makes_no_external_calls(client, fake_ors):
    first = get_route(client, **CHI_DEN).json()
    second = get_route(client, **CHI_DEN).json()
    assert second["meta"]["external_calls"] == 0 and second["meta"]["route_source"] == "stored"
    assert first["meta"]["route_source"] == "api"
    assert second["fuel"] == first["fuel"]
    assert len(fake_ors.calls) == 1


def test_different_spelling_of_same_cities_reuses_the_stored_route(client, fake_ors):
    get_route(client, **CHI_DEN)
    again = get_route(client, start="chicago illinois", finish="DENVER, colorado, USA").json()
    assert again["meta"]["external_calls"] == 0


def test_changing_corridor_reuses_the_stored_route(client, fake_ors):
    get_route(client, **CHI_DEN)
    wide = get_route(client, **CHI_DEN, corridor_miles=30).json()
    assert wide["meta"]["external_calls"] == 0
    assert wide["assumptions"]["corridor_miles_used"] == 30
    assert "corridor_miles=30" in wide["map_url"]


def test_map_page_adds_no_external_calls(client, fake_ors):
    api = get_route(client, **CHI_DEN).json()
    page = client.get(reverse("route-map"), CHI_DEN)
    assert page.status_code == 200
    html = page.content.decode()
    assert "leaflet" in html and 'id="route-geojson"' in html and f"${api['fuel']['total_cost']}" in html
    assert len(fake_ors.calls) == 1


# --- input resolution --------------------------------------------------------


def test_coordinates_resolve_locally(client, fake_ors):
    body = get_route(client, start="41.837045,-87.684939", finish="39.76185, -104.881105").json()
    assert body["start"]["resolved_by"] == body["finish"]["resolved_by"] == "coordinates"
    assert body["meta"]["external_calls"] == 1


def test_street_address_uses_geocoder_once_then_stored_result(client, fake_ors):
    fake_ors.places["1437 bannock st, denver"] = (39.76185, -104.881105, "1437 Bannock St, Denver, CO, USA")
    body = get_route(client, start="Chicago, IL", finish="1437 Bannock St, Denver").json()
    assert body["finish"] == {
        "query": "1437 Bannock St, Denver",
        "label": "1437 Bannock St, Denver, CO, USA",
        "lat": 39.76185,
        "lon": -104.881105,
        "resolved_by": "geocoder",
    }
    assert body["meta"]["external_calls"] == 2
    assert fake_ors.calls[0]["params"] == {"text": "1437 Bannock St, Denver", "boundary.country": "US", "size": 1}
    again = get_route(client, start="Chicago, IL", finish="1437 Bannock St, Denver").json()
    assert again["meta"]["external_calls"] == 0


def test_unknown_place_is_400(client, fake_ors):
    response = get_route(client, start="Nowhereville Xyz", finish="Denver, CO")
    assert response.status_code == 400 and error_code(response) == "location_not_found"
    assert response.json()["error"]["details"] == {"query": "Nowhereville Xyz"}


def test_geocoder_result_outside_usa_is_400(client, fake_ors):
    fake_ors.places["union station toronto"] = (43.6453, -79.3806, "Union Station, Toronto, ON, Canada")
    response = get_route(client, start="Union Station Toronto", finish="Denver, CO")
    assert response.status_code == 400 and error_code(response) == "location_outside_usa"


@pytest.mark.parametrize(
    "start", ["Toronto, ON", "Vancouver, BC", "Monterrey, Mexico", "49.28,-123.12", "19.43,-99.13"]
)
def test_foreign_input_is_rejected_without_calls(client, fake_ors, start):
    response = get_route(client, start=start, finish="Denver, CO")
    assert response.status_code == 400 and error_code(response) == "location_outside_usa"
    assert fake_ors.calls == []


@pytest.mark.parametrize(
    "params, field",
    [
        ({"start": "Chicago, IL"}, "finish"),
        ({"finish": "Denver, CO"}, "start"),
        ({"start": "", "finish": "Denver, CO"}, "start"),
        ({**CHI_DEN, "corridor_miles": "abc"}, "corridor_miles"),
        ({**CHI_DEN, "corridor_miles": 0}, "corridor_miles"),
        ({**CHI_DEN, "corridor_miles": 500}, "corridor_miles"),
        ({"start": "x" * 201, "finish": "Denver, CO"}, "start"),
    ],
)
def test_invalid_parameters_are_400(client, fake_ors, params, field):
    response = get_route(client, **params)
    assert response.status_code == 400 and error_code(response) == "invalid_request"
    assert field in response.json()["error"]["details"]["fields"]
    assert fake_ors.calls == []


def test_same_start_and_finish_is_a_free_trip(client, fake_ors):
    fake_ors.responses["driving-hgv"] = FakeResponse(
        200,
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[-87.684939, 41.837045], [-87.684939, 41.837045]],
                    },
                    "properties": {"summary": {"distance": 0, "duration": 0}},
                }
            ],
        },
    )
    body = get_route(client, start="Chicago, IL", finish="Chicago, IL").json()
    assert body["fuel"]["stop_count"] == 0 and body["fuel"]["total_cost"] == 0


# --- routing provider failures -------------------------------------------


def test_truck_route_missing_falls_back_to_car(client, fake_ors):
    fake_ors.responses["driving-hgv"] = ors_error(
        404, 2009, "Route could not be found - Unable to find a route between points 1 and 2."
    )
    body = get_route(client, **CHI_DEN).json()
    assert body["route"]["profile"] == "driving-car"
    assert body["meta"]["external_calls"] == 2
    assert [c["url"].split("/")[-2] for c in fake_ors.calls] == ["driving-hgv", "driving-car"]


def test_no_route_by_truck_or_car_is_422(client, fake_ors):
    response = get_route(client, start="Honolulu, HI", finish="Chicago, IL")  # no fixture: unroutable
    assert response.status_code == 422 and error_code(response) == "route_not_found"
    assert len(fake_ors.directions_calls) == 2


def test_route_longer_than_provider_limit_is_422(client, fake_ors):
    limit = ors_error(
        400,
        2004,
        "Request parameters exceed the server configuration limits. "
        "The approximated route distance must not be greater than 6000000.0 meters.",
    )
    fake_ors.responses.update({"driving-hgv": limit, "driving-car": limit})
    response = get_route(client, **CHI_DEN)
    assert response.status_code == 422 and "exceed" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "reply, status, code, message",
    [
        (FakeResponse(429, {"error": "Rate limit exceeded"}), 502, "routing_service_error", "quota"),
        (FakeResponse(500, {"error": {"code": 2099, "message": "Unknown internal error"}}), 422, "route_not_found", ""),
        (FakeResponse(502, None, text="<html>Bad Gateway</html>"), 502, "routing_service_error", "(502)"),
        (FakeResponse(503, {"error": "Service unavailable"}), 502, "routing_service_error", "(503)"),
        (FakeResponse(200, {"type": "FeatureCollection", "features": []}), 502, "routing_service_error", "Unexpected"),
        (FakeResponse(401, {"error": "Unauthorized"}), 503, "routing_not_configured", "rejected the API key"),
    ],
)
def test_provider_errors_map_to_clear_responses(client, fake_ors, reply, status, code, message):
    fake_ors.responses.update({"driving-hgv": reply, "driving-car": reply})
    response = get_route(client, **CHI_DEN)
    assert response.status_code == status
    assert error_code(response) == code
    assert message in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "exc, message",
    [
        (requests.Timeout("read timed out"), "timed out"),
        (requests.ConnectionError("connection refused"), "Could not reach"),
    ],
)
def test_network_failures_are_502(client, fake_ors, exc, message):
    fake_ors.error = exc
    response = get_route(client, **CHI_DEN)
    assert response.status_code == 502 and message in response.json()["error"]["message"]


def test_transient_failures_are_not_stored(client, fake_ors):
    fake_ors.error = requests.ConnectionError("down")
    assert get_route(client, **CHI_DEN).status_code == 502
    fake_ors.error = None
    body = get_route(client, **CHI_DEN).json()
    assert body["meta"]["external_calls"] == 1


def test_wrong_api_key_is_503(client, fake_ors, settings):
    settings.ORS_API_KEY = "not-the-dummy-key"
    response = get_route(client, **CHI_DEN)
    assert response.status_code == 503 and "rejected the API key" in response.json()["error"]["message"]


def test_missing_api_key_is_503_without_calls(client, fake_ors, settings):
    settings.ORS_API_KEY = ""
    response = get_route(client, **CHI_DEN)
    assert response.status_code == 503 and error_code(response) == "routing_not_configured"
    assert fake_ors.calls == []


def test_stored_route_still_works_without_a_key(client, fake_ors, settings):
    get_route(client, **CHI_DEN)
    settings.ORS_API_KEY = ""
    assert get_route(client, **CHI_DEN).status_code == 200


def test_map_page_shows_errors(client, fake_ors):
    assert "start and finish" in client.get(reverse("route-map")).content.decode()
    page = client.get(reverse("route-map"), {"start": "Toronto, ON", "finish": "Denver, CO"})
    assert page.status_code == 400 and "outside the USA" in page.content.decode()


def test_root_redirects_to_an_example_map(client, fake_ors):
    response = client.get("/")
    assert response.status_code == 302
    assert response["Location"] == "/api/v1/route/map/?start=Chicago%2C+IL&finish=Denver%2C+CO"
    assert client.get(response["Location"]).status_code == 200


def test_openapi_schema_and_swagger_ui(client):
    schema = client.get("/api/schema/", HTTP_ACCEPT="application/vnd.oai.openapi+json")
    assert schema.status_code == 200
    spec = schema.json()
    route = spec["paths"]["/api/v1/route/"]
    assert {p["name"] for p in route["get"]["parameters"]} == {"start", "finish", "corridor_miles"}
    assert {"200", "400", "422", "502", "503"} <= set(route["get"]["responses"])
    assert "post" in route
    assert client.get("/api/docs/").status_code == 200
