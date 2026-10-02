"""Geometry helpers for matching fuel stations to a driving route.

All inputs are (lat, lon) degrees. Distances are statute miles.
"""

from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

EARTH_RADIUS_MILES = 3958.7613


def haversine_miles(lat1, lon1, lat2, lon2):
    """Vectorised great-circle distance."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def to_unit_xyz(lat, lon):
    """Points on the unit sphere, so a KD-tree measures (chord) distance correctly anywhere."""
    lat, lon = np.radians(lat), np.radians(lon)
    return np.column_stack((np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)))


def chord_to_miles(chord):
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.clip(chord / 2, 0, 1))


def miles_to_chord(miles):
    return 2 * np.sin(miles / (2 * EARTH_RADIUS_MILES))


def cumulative_miles(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Distance along the polyline at each vertex, starting at 0."""
    if len(lat) < 2:
        return np.zeros(len(lat))
    steps = haversine_miles(lat[:-1], lon[:-1], lat[1:], lon[1:])
    return np.concatenate(([0.0], np.cumsum(steps)))


@dataclass
class RouteLine:
    """A polyline resampled so no two consecutive points are more than ``spacing`` miles apart."""

    lat: np.ndarray
    lon: np.ndarray
    miles: np.ndarray  # distance along the route at each point

    @property
    def length_miles(self) -> float:
        return float(self.miles[-1]) if len(self.miles) else 0.0

    @classmethod
    def from_lonlat(cls, coordinates, spacing_miles: float = 0.5, scale_to_miles: float | None = None) -> "RouteLine":
        """Build from GeoJSON-ordered [lon, lat] pairs.

        Long straight segments are densified so nearest-point lookups are accurate
        to within ``spacing / 2``. ``scale_to_miles`` rescales mile markers so they
        agree with the routing engine's reported distance.
        """
        coords = np.asarray(coordinates, dtype=float)
        if coords.ndim != 2 or len(coords) < 2:
            raise ValueError("A route needs at least two coordinates.")
        lon, lat = coords[:, 0], coords[:, 1]
        seg = haversine_miles(lat[:-1], lon[:-1], lat[1:], lon[1:])

        # Insert evenly spaced points into segments longer than the spacing.
        pieces = np.maximum(1, np.ceil(seg / spacing_miles).astype(int))
        t = np.concatenate([np.arange(n) / n for n in pieces])
        start = np.repeat(np.arange(len(seg)), pieces)
        new_lat = np.append(lat[start] + (lat[start + 1] - lat[start]) * t, lat[-1])
        new_lon = np.append(lon[start] + (lon[start + 1] - lon[start]) * t, lon[-1])

        miles = cumulative_miles(new_lat, new_lon)
        if scale_to_miles and miles[-1] > 0:
            miles *= scale_to_miles / miles[-1]
        return cls(lat=new_lat, lon=new_lon, miles=miles)


@dataclass(frozen=True)
class CorridorMatch:
    index: np.ndarray  # positions into the station arrays that were passed in
    mile: np.ndarray  # mile marker of each station's closest route point
    off_route_miles: np.ndarray  # straight-line distance from the station to the route


def match_stations_to_route(
    route: RouteLine, station_lat: np.ndarray, station_lon: np.ndarray, corridor_miles: float
) -> CorridorMatch:
    """Find stations within ``corridor_miles`` of the route and where along it they sit."""
    empty = CorridorMatch(np.array([], dtype=int), np.array([]), np.array([]))
    if len(station_lat) == 0:
        return empty

    # Cheap bounding-box prefilter before the KD-tree query.
    pad_lat = corridor_miles / 69.0
    pad_lon = corridor_miles / max(1e-6, 69.0 * np.cos(np.radians(min(80.0, np.abs(route.lat).max()))))
    in_box = np.flatnonzero(
        (station_lat >= route.lat.min() - pad_lat)
        & (station_lat <= route.lat.max() + pad_lat)
        & (station_lon >= route.lon.min() - pad_lon)
        & (station_lon <= route.lon.max() + pad_lon)
    )
    if len(in_box) == 0:
        return empty

    tree = cKDTree(to_unit_xyz(route.lat, route.lon))
    chord, nearest = tree.query(
        to_unit_xyz(station_lat[in_box], station_lon[in_box]),
        distance_upper_bound=miles_to_chord(corridor_miles),
    )
    hit = np.isfinite(chord)
    return CorridorMatch(
        index=in_box[hit],
        mile=route.miles[nearest[hit]],
        off_route_miles=chord_to_miles(chord[hit]),
    )


def simplify(lat: np.ndarray, lon: np.ndarray, tolerance_miles: float) -> np.ndarray:
    """Ramer-Douglas-Peucker; returns the indices of the points to keep.

    Used to shrink the geometry sent to clients. Planar approximation in a local
    equirectangular projection, which is fine at map-display tolerances.
    """
    n = len(lat)
    if n <= 2:
        return np.arange(n)
    x = np.radians(lon) * np.cos(np.radians(lat.mean())) * EARTH_RADIUS_MILES
    y = np.radians(lat) * EARTH_RADIUS_MILES
    keep = np.zeros(n, dtype=bool)
    keep[[0, n - 1]] = True
    stack = [(0, n - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        dx, dy = x[last] - x[first], y[last] - y[first]
        px, py = x[first + 1 : last] - x[first], y[first + 1 : last] - y[first]
        length = np.hypot(dx, dy)
        if length == 0:
            dist = np.hypot(px, py)
        else:
            dist = np.abs(dx * py - dy * px) / length
        worst = int(np.argmax(dist))
        if dist[worst] > tolerance_miles:
            split = first + 1 + worst
            keep[split] = True
            stack.append((first, split))
            stack.append((split, last))
    return np.flatnonzero(keep)
