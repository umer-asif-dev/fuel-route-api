"""In-memory station index, loaded once per process.

Requests never touch the database: station coordinates and prices live in
numpy arrays so corridor matching is a vectorised operation.
"""

import threading
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class StationInfo:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    price: float
    lat: float
    lon: float


@dataclass(frozen=True)
class StationIndex:
    stations: tuple[StationInfo, ...]
    lat: np.ndarray
    lon: np.ndarray
    price: np.ndarray

    def __len__(self) -> int:
        return len(self.stations)

    @classmethod
    def from_infos(cls, infos: list[StationInfo]) -> "StationIndex":
        return cls(
            stations=tuple(infos),
            lat=np.array([s.lat for s in infos], dtype=float),
            lon=np.array([s.lon for s in infos], dtype=float),
            price=np.array([s.price for s in infos], dtype=float),
        )


_lock = threading.Lock()
_index: StationIndex | None = None


def get_station_index() -> StationIndex:
    global _index
    if _index is None:
        with _lock:
            if _index is None:
                from stations.models import FuelStation

                rows = FuelStation.objects.values_list(
                    "opis_id", "name", "address", "city", "state", "retail_price", "latitude", "longitude"
                )
                _index = StationIndex.from_infos(
                    [StationInfo(i, n, a, c, s, float(p), la, lo) for i, n, a, c, s, p, la, lo in rows]
                )
    return _index


def reset_station_index() -> None:
    """Drop the cached index (after re-importing stations, or in tests)."""
    global _index
    with _lock:
        _index = None
