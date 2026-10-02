import gzip
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
from django.core.management import call_command
from django.test import override_settings

from stations.services.index import reset_station_index

FIXTURES = Path(__file__).parent / "fixtures"
DUMMY_KEY = "dummy-ors-key"


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker, tmp_path_factory):
    """Load the real station data once per test session (offline, from committed files)."""
    export = tmp_path_factory.mktemp("export") / "stations.csv"
    with django_db_blocker.unblock(), override_settings(STATIONS_GEOCODED_CSV=export):
        call_command("import_stations", "--offline", stdout=io.StringIO())


@pytest.fixture(autouse=True)
def _isolate(settings):
    settings.ORS_API_KEY = DUMMY_KEY
    reset_station_index()
    yield
    reset_station_index()


def load_route_fixture(name: str) -> dict:
    with gzip.open(FIXTURES / f"ors_{name}.json.gz", "rt") as fh:
        return json.load(fh)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self._text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("No JSON body")
        return self._payload


def ors_error(status, code, message):
    return FakeResponse(status, {"error": {"code": code, "message": message}, "info": {"engine": {"version": "9.0.0"}}})


class FakeORS:
    """Behaves like OpenRouteService at the HTTP layer and records every call.

    * Requires ``Authorization: <DUMMY_KEY>`` (403 otherwise, like ORS).
    * Directions: serves recorded road geometry when the request's [lon, lat]
      endpoints match a fixture; otherwise 404 with ORS error code 2010.
    * Geocode: answers from ``self.places``; unknown text returns no features.
    * ``self.responses[profile]`` overrides a profile's reply; ``self.error``
      raises a transport exception for every call.
    """

    PROFILES = {"driving-hgv", "driving-car"}
    ROUTES = ("chicago_denver", "chicago_houston", "nyc_la", "seattle_miami", "dallas_austin")

    def __init__(self):
        self.calls = []
        self.routes = [load_route_fixture(name) for name in self.ROUTES]
        self.places = {}
        self.responses = {}
        self.error = None

    def __call__(self, session, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.error:
            raise self.error
        if session.headers.get("Authorization") != DUMMY_KEY:
            return FakeResponse(403, {"error": "Access to this API has been disallowed"})
        if "/geocode/search" in url:
            return self._geocode(kwargs["params"])
        return self._directions(url, kwargs["json"])

    def _directions(self, url, body):
        profile = url.split("/v2/directions/")[1].split("/")[0]
        if profile not in self.PROFILES:
            return ors_error(400, 2003, f"Unknown profile {profile}")
        if profile in self.responses:
            return self.responses[profile]
        (lon1, lat1), (lon2, lat2) = body["coordinates"]
        assert -180 <= lon1 <= 180 and -90 <= lat1 <= 90, "coordinates must be [lon, lat]"
        for route in self.routes:
            (qlon1, qlat1), (qlon2, qlat2) = route["metadata"]["query"]["coordinates"]
            if max(abs(lon1 - qlon1), abs(lat1 - qlat1), abs(lon2 - qlon2), abs(lat2 - qlat2)) < 0.01:
                return FakeResponse(200, route)
        return ors_error(
            404, 2010, "Could not find routable point within a radius of 350.0 meters of specified coordinate."
        )

    def _geocode(self, params):
        assert params.get("boundary.country") == "US"
        hit = self.places.get(params["text"].strip().lower())
        features = (
            []
            if hit is None
            else [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [hit[1], hit[0]]},
                    "properties": {"label": hit[2], "country_a": "USA"},
                }
            ]
        )
        return FakeResponse(200, {"type": "FeatureCollection", "features": features})

    @property
    def directions_calls(self):
        return [c for c in self.calls if "/v2/directions/" in c["url"]]


@pytest.fixture
def fake_ors():
    fake = FakeORS()
    with patch.object(requests.Session, "request", autospec=True, side_effect=fake):
        yield fake
