"""Is a point in the USA? Uses the Census Bureau's 1:20M national boundary.

A bounding box is not enough (Toronto and Monterrey sit inside the box around
the lower 48), so we test against the real border polygons, with a few miles
of tolerance for coastline simplification.
"""

import json
import struct
import threading
from pathlib import Path

import numpy as np
from django.conf import settings
from scipy.spatial import cKDTree

from routing.services.route_geometry import chord_to_miles, to_unit_xyz

COAST_TOLERANCE_MILES = 5.0


def rings_from_shapefile(shp_path: Path) -> list[list[list[float]]]:
    """Read polygon rings ([lon, lat] lists) from an ESRI .shp file of Polygon records."""
    data = shp_path.read_bytes()
    rings, offset = [], 100  # skip the file header
    while offset < len(data):
        _, length = struct.unpack(">2i", data[offset : offset + 8])
        record = data[offset + 8 : offset + 8 + length * 2]
        offset += 8 + length * 2
        if struct.unpack("<i", record[:4])[0] != 5:  # 5 = Polygon
            continue
        num_parts, num_points = struct.unpack("<2i", record[36:44])
        parts = list(struct.unpack(f"<{num_parts}i", record[44 : 44 + 4 * num_parts])) + [num_points]
        points = np.frombuffer(record, dtype="<f8", count=num_points * 2, offset=44 + 4 * num_parts).reshape(-1, 2)
        for a, b in zip(parts[:-1], parts[1:], strict=True):
            rings.append(np.round(points[a:b], 4).tolist())
    return rings


class USBoundary:
    def __init__(self, rings: list[list[list[float]]]):
        self.rings = [np.asarray(r, dtype=float) for r in rings]
        self.bboxes = np.array([[r[:, 0].min(), r[:, 1].min(), r[:, 0].max(), r[:, 1].max()] for r in self.rings])
        vertices = np.concatenate(self.rings)
        self._tree = cKDTree(to_unit_xyz(vertices[:, 1], vertices[:, 0]))

    @classmethod
    def from_json(cls, path: Path) -> "USBoundary":
        return cls(json.loads(path.read_text())["rings"])

    def contains(self, lat: float, lon: float) -> bool:
        """Even-odd ray casting over every ring, so lakes/holes are handled too."""
        inside = False
        candidates = np.flatnonzero(
            (self.bboxes[:, 0] <= lon)
            & (lon <= self.bboxes[:, 2])
            & (self.bboxes[:, 1] <= lat)
            & (lat <= self.bboxes[:, 3])
        )
        for k in candidates:
            ring = self.rings[k]
            x1, y1 = ring[:, 0], ring[:, 1]
            x2, y2 = np.roll(x1, -1), np.roll(y1, -1)
            crosses = (y1 > lat) != (y2 > lat)
            with np.errstate(divide="ignore", invalid="ignore"):
                x_at = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            inside ^= bool(np.count_nonzero(crosses & (lon < x_at)) % 2)
        return inside

    def distance_to_border_miles(self, lat: float, lon: float) -> float:
        chord, _ = self._tree.query(to_unit_xyz(np.array([lat]), np.array([lon]))[0])
        return float(chord_to_miles(chord))

    def includes(self, lat: float, lon: float) -> bool:
        return self.contains(lat, lon) or self.distance_to_border_miles(lat, lon) <= COAST_TOLERANCE_MILES


_boundary: USBoundary | None = None
_lock = threading.Lock()


def get_us_boundary() -> USBoundary:
    global _boundary
    if _boundary is None:
        with _lock:
            if _boundary is None:
                _boundary = USBoundary.from_json(settings.US_BOUNDARY_JSON)
    return _boundary
