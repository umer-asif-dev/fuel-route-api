"""Thin OpenRouteService client: one directions call per route, geocoding only as a fallback.

Docs: https://openrouteservice.org/dev/#/api-docs
"""

from dataclasses import dataclass

import requests

from routing.exceptions import LocationNotFound, RouteNotFound, RoutingNotConfigured, RoutingServiceError

METERS_PER_MILE = 1609.344

# ORS error codes meaning "these points can't be connected", as opposed to an outage.
_UNROUTABLE_CODES = {2004, 2009, 2010, 2099}


@dataclass(frozen=True)
class RouteResult:
    coordinates: list[list[float]]  # GeoJSON order: [lon, lat]
    distance_miles: float
    duration_hours: float
    profile: str


@dataclass(frozen=True)
class GeocodeResult:
    lat: float
    lon: float
    label: str


class ORSClient:
    def __init__(self, api_key: str, base_url: str = "https://api.openrouteservice.org", timeout_s: float = 15):
        if not api_key:
            raise RoutingNotConfigured("ORS_API_KEY is not set. Add a free OpenRouteService key to .env.")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update({"Authorization": api_key, "Accept": "application/json, application/geo+json"})
        self.calls_made = 0

    def directions(self, start: tuple[float, float], finish: tuple[float, float], profile: str) -> RouteResult:
        """Driving route between two (lat, lon) points."""
        payload = {
            "coordinates": [[start[1], start[0]], [finish[1], finish[0]]],
            "instructions": False,
            # Snap to the nearest road however far it is; inputs are often city centroids.
            "radiuses": [-1, -1],
        }
        data = self._request("POST", f"/v2/directions/{profile}/geojson", json=payload)
        try:
            feature = data["features"][0]
            summary = feature["properties"]["summary"]
            return RouteResult(
                coordinates=feature["geometry"]["coordinates"],
                distance_miles=summary.get("distance", 0) / METERS_PER_MILE,
                duration_hours=summary.get("duration", 0) / 3600,
                profile=profile,
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise RoutingServiceError("Unexpected response from the routing service.") from exc

    def geocode(self, text: str) -> GeocodeResult:
        data = self._request("GET", "/geocode/search", params={"text": text, "boundary.country": "US", "size": 1})
        features = data.get("features") or []
        if not features:
            raise LocationNotFound(f"Could not find a US location matching '{text}'.", query=text)
        lon, lat = features[0]["geometry"]["coordinates"][:2]
        return GeocodeResult(lat=lat, lon=lon, label=features[0].get("properties", {}).get("label", text))

    def _request(self, method: str, path: str, **kwargs) -> dict:
        self.calls_made += 1
        try:
            response = self.session.request(method, self.base_url + path, timeout=self.timeout_s, **kwargs)
        except requests.Timeout as exc:
            raise RoutingServiceError("The routing service timed out.") from exc
        except requests.RequestException as exc:
            raise RoutingServiceError("Could not reach the routing service.") from exc

        if response.ok:
            return response.json()

        error = _error_details(response)
        if response.status_code in (401, 403):
            raise RoutingNotConfigured("The routing service rejected the API key. Check ORS_API_KEY.")
        if response.status_code == 429:
            raise RoutingServiceError("Routing service quota exceeded; try again later.")
        if error.get("code") in _UNROUTABLE_CODES or response.status_code == 404:
            raise RouteNotFound(
                error.get("message") or "No drivable route between these locations.", ors_code=error.get("code")
            )
        raise RoutingServiceError(f"Routing service error ({response.status_code}).", ors_message=error.get("message"))


def _error_details(response: requests.Response) -> dict:
    try:
        error = response.json().get("error", {})
    except ValueError:
        return {}
    return error if isinstance(error, dict) else {"message": str(error)}
