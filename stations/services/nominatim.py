"""One-time fallback geocoder for the few station cities missing from the Gazetteer.

Only used by the `import_stations` management command, never at request time.
Results (including misses) are cached in a committed JSON file so re-imports
are fully offline. Follows the Nominatim usage policy: max 1 request/second
and an identifying User-Agent.
"""

import json
import time
from pathlib import Path

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "fuel-route-api/0.1 (Spotter backend assessment)"


class CachedNominatim:
    def __init__(self, cache_path: Path, min_interval_s: float = 1.1, timeout_s: float = 10):
        self.cache_path = cache_path
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self._last_call = 0.0
        self._cache: dict[str, list[float] | None] = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        self.calls_made = 0

    @staticmethod
    def _key(city: str, state: str) -> str:
        return f"{city.upper()}|{state.upper()}"

    def lookup(self, city: str, state: str, allow_network: bool = True) -> tuple[float, float] | None:
        key = self._key(city, state)
        if key in self._cache:
            hit = self._cache[key]
            return (hit[0], hit[1]) if hit else None
        if not allow_network:
            return None

        wait = self.min_interval_s - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
        self.calls_made += 1

        response = requests.get(
            NOMINATIM_URL,
            params={"city": city, "state": state, "countrycodes": "us", "format": "jsonv2", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        results = response.json()
        coords = [float(results[0]["lat"]), float(results[0]["lon"])] if results else None
        self._cache[key] = coords
        return (coords[0], coords[1]) if coords else None

    def save(self) -> None:
        self.cache_path.write_text(json.dumps(self._cache, indent=1, sort_keys=True) + "\n")
