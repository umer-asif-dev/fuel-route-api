"""Turn user input ("Chicago, IL", "41.88,-87.63", "1600 Pennsylvania Ave") into coordinates.

"City, ST" and raw coordinates resolve locally with no API call; anything else
falls back to the routing provider's geocoder.
"""

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass

from django.conf import settings

from routing.exceptions import LocationNotFound, LocationOutsideUSA
from routing.services.us_boundary import get_us_boundary
from stations.services.geocode import Gazetteer

STATE_NAMES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}

_STATE_CODES = frozenset(STATE_NAMES.values())

# Inputs that clearly name a place outside the USA; rejected before any geocoding,
# since a US-only geocoder would otherwise return a namesake (Toronto, OH).
_FOREIGN_REGION = re.compile(
    r",\s*(AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT|BCN|CHIH|COAH|NL|SON|TAMPS|"
    r"canada|mexico|m[eé]xico|ontario|quebec|qu[eé]bec|british columbia|alberta)\.?\s*$",
    re.IGNORECASE,
)

_COORDS = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_COUNTRY_SUFFIX = re.compile(r",?\s*(usa|us|united states( of america)?)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class ResolvedLocation:
    query: str
    lat: float
    lon: float
    label: str
    source: str  # "coordinates" | "gazetteer" | "geocoder"

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "label": self.label,
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
            "resolved_by": self.source,
        }


def in_usa(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180 and get_us_boundary().includes(lat, lon)


_gazetteer: Gazetteer | None = None
_gazetteer_lock = threading.Lock()


def get_gazetteer() -> Gazetteer:
    global _gazetteer
    if _gazetteer is None:
        with _gazetteer_lock:
            if _gazetteer is None:
                _gazetteer = Gazetteer.from_csv(settings.GAZETTEER_CSV)
    return _gazetteer


def parse_city_state(text: str) -> tuple[str, str] | None:
    """Split "City, ST" / "City, State" / "City ST" into (city, state code)."""
    text = _COUNTRY_SUFFIX.sub("", text.strip())
    if "," in text:
        city, _, state = text.rpartition(",")
    else:
        city, _, state = text.rpartition(" ")
    city, state = city.strip(), state.strip().rstrip(".")
    if not city:
        return None
    if len(state) == 2:
        code = state.upper() if state.upper() in _STATE_CODES else None
    else:
        code = STATE_NAMES.get(state.lower())
    return (city, code) if code else None


def resolve_location(text: str, geocode: Callable[[str], tuple[float, float, str]]) -> ResolvedLocation:
    """Resolve without an API call when possible; ``geocode`` is the fallback."""
    text = text.strip()
    if not text:
        raise LocationNotFound("Location is empty.")

    if _FOREIGN_REGION.search(_COUNTRY_SUFFIX.sub("", text)) or re.search(
        r"(?<!new )\b(canada|mexico)\s*$", text, re.I
    ):
        raise LocationOutsideUSA(f"'{text}' is outside the USA; start and finish must both be in the USA.", query=text)

    if match := _COORDS.match(text):
        lat, lon = float(match.group(1)), float(match.group(2))
        location = ResolvedLocation(text, lat, lon, f"{lat:.5f}, {lon:.5f}", "coordinates")
    elif (parsed := parse_city_state(text)) and (coords := get_gazetteer().lookup(*parsed)):
        location = ResolvedLocation(text, coords[0], coords[1], f"{parsed[0].title()}, {parsed[1]}", "gazetteer")
    else:
        lat, lon, label = geocode(text)
        location = ResolvedLocation(text, lat, lon, label, "geocoder")

    if not in_usa(location.lat, location.lon):
        raise LocationOutsideUSA(f"'{text}' is outside the USA; start and finish must both be in the USA.", query=text)
    return location
