"""Offline geocoding of US "City, ST" names using Census Gazetteer centroids.

The fuel price CSV has no coordinates and its addresses are highway exits
("I-44, EXIT 283 & US-69"), so the city is the only reliable location signal.
Each station is placed at its city's internal point, which is accurate enough
for matching stations to a route within a corridor of a few miles.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path

# Two-letter USPS codes for the 50 states plus DC.
US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)

# Census name suffixes that describe the place type, not its name.
_PLACE_SUFFIXES = re.compile(
    r"\s+(city and borough|consolidated government|unified government|metro government|"
    r"metropolitan government|urban county|city|town|village|borough|cdp|municipality|"
    r"township|charter township|plantation|gore|grant|location|purchase|ccd|"
    r"unorganized territory|precinct|district|comunidad|zona urbana)"
    r"(\s*\(balance\))?$",
    re.IGNORECASE,
)
# Ranks are tier * 10**12 - land area, with tiers: incorporated place, CDP, county
# subdivision. Name aliases ("Urban Honolulu" -> "Honolulu", "Johnson City" -> "Johnson")
# sit half a tier lower: they lose to an exact name of the same kind but beat a lower
# tier, so "Honolulu" isn't sent to the Honolulu subdivision in the remote NW islands.
_ALIAS_PENALTY = 5 * 10**11
# Consolidated city-county governments ("Augusta-Richmond County") have county-wide
# centroids, so their short name is only used when nothing else matches.
_CONSOLIDATED_PENALTY = 5 * 10**12

_WORD_ALIASES = [
    (re.compile(r"\bsaint\b"), "st"),
    (re.compile(r"\bsainte\b"), "ste"),
    (re.compile(r"\bfort\b"), "ft"),
    (re.compile(r"\bmount\b"), "mt"),
]


def normalize_city(name: str) -> str:
    """Return a lookup key that ignores case, punctuation, spacing and common abbreviations.

    "Mc Calla", "McCalla" and "MC CALLA" all become "mccalla";
    "Saint Joseph" and "St. Joseph" both become "stjoseph".
    """
    key = name.strip().lower().replace("-", " ").replace(".", " ")
    for pattern, replacement in _WORD_ALIASES:
        key = pattern.sub(replacement, key)
    return re.sub(r"[^a-z0-9]", "", key)


def strip_place_suffix(census_name: str) -> str:
    return _PLACE_SUFFIXES.sub("", census_name.strip())


@dataclass(frozen=True)
class Place:
    lat: float
    lon: float
    rank: int  # lower is preferred when two places share a normalized name


class Gazetteer:
    """In-memory (state, normalized city) -> coordinates lookup."""

    def __init__(self, places: dict[tuple[str, str], Place] | None = None):
        self._places: dict[tuple[str, str], Place] = places or {}

    def __len__(self) -> int:
        return len(self._places)

    def add(self, state: str, name: str, lat: float, lon: float, rank: int) -> None:
        key = (state.upper(), normalize_city(name))
        existing = self._places.get(key)
        if existing is None or rank < existing.rank:
            self._places[key] = Place(lat, lon, rank)

    def lookup(self, city: str, state: str) -> tuple[float, float] | None:
        place = self._places.get((state.strip().upper(), normalize_city(city)))
        return (place.lat, place.lon) if place else None

    @classmethod
    def from_census_files(cls, place_file: Path, cousub_file: Path | None = None) -> "Gazetteer":
        """Build from raw Census Gazetteer files (pipe-delimited).

        Incorporated places win over CDPs, which win over county subdivisions;
        within a tier, the larger land area wins.
        """
        gaz = cls()
        sources = [(place_file, 0)] + ([(cousub_file, 2)] if cousub_file else [])
        for path, base_rank in sources:
            with open(path, encoding="utf-8") as fh:
                for row in csv.DictReader(fh, delimiter="|"):
                    row = {k.strip(): v.strip() for k, v in row.items() if k}
                    state = row["USPS"]
                    if state not in US_STATES:
                        continue
                    rank = base_rank + (1 if row["NAME"].endswith(" CDP") else 0)
                    # Larger places sort first inside a tier.
                    rank = rank * 10**12 - int(row["ALAND"] or 0)
                    lat, lon = float(row["INTPTLAT"]), float(row["INTPTLONG"])
                    name = strip_place_suffix(row["NAME"])
                    gaz.add(state, name, lat, lon, rank)
                    # Consolidated governments ("Nashville-Davidson metropolitan
                    # government", "Macon-Bibb County") are known by their first part.
                    if "-" in name:
                        gaz.add(state, name.split("-")[0], lat, lon, rank + _CONSOLIDATED_PENALTY)
                    # Hawaii's Census name for Honolulu is "Urban Honolulu".
                    if name.startswith("Urban "):
                        gaz.add(state, name[len("Urban ") :], lat, lon, rank + _ALIAS_PENALTY)
                    # "Boise City city" is usually written as "Boise".
                    if name.endswith(" City"):
                        gaz.add(state, name[: -len(" City")], lat, lon, rank + _ALIAS_PENALTY)
        return gaz

    @classmethod
    def from_csv(cls, path: Path) -> "Gazetteer":
        """Load the compact committed file written by `to_csv` (state,key,lat,lon)."""
        places = {}
        with open(path, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                places[(row["state"], row["key"])] = Place(float(row["lat"]), float(row["lon"]), 0)
        return cls(places)

    def to_csv(self, path: Path) -> None:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["state", "key", "lat", "lon"])
            for (state, key), place in sorted(self._places.items()):
                writer.writerow([state, key, f"{place.lat:.6f}", f"{place.lon:.6f}"])
