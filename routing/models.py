"""Locally stored OpenRouteService results, so each trip or address is fetched once."""

import json
import zlib

from django.db import models


class LookupStatus(models.TextChoices):
    OK = "ok", "OK"
    NOT_FOUND = "not_found", "Not found"


class StoredRoute(models.Model):
    """One driving route between two points, keyed by coordinates rounded to ~11 m.

    Coordinates are stored as integers (degrees x 10,000) so equality is exact.
    """

    start_lat_e4 = models.IntegerField()
    start_lon_e4 = models.IntegerField()
    finish_lat_e4 = models.IntegerField()
    finish_lon_e4 = models.IntegerField()
    status = models.CharField(max_length=16, choices=LookupStatus.choices, default=LookupStatus.OK)
    profile = models.CharField(max_length=32, blank=True)
    distance_miles = models.FloatField(null=True, blank=True)
    duration_hours = models.FloatField(null=True, blank=True)
    geometry_zlib = models.BinaryField(null=True, blank=True)  # zlib-compressed JSON [[lon, lat], ...]
    error_message = models.TextField(blank=True)
    fetched_at = models.DateTimeField()
    last_used_at = models.DateTimeField()
    hit_count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["start_lat_e4", "start_lon_e4", "finish_lat_e4", "finish_lon_e4"],
                name="unique_route_endpoints",
            )
        ]

    def __str__(self) -> str:
        start = f"{self.start_lat_e4 / 1e4}, {self.start_lon_e4 / 1e4}"
        finish = f"{self.finish_lat_e4 / 1e4}, {self.finish_lon_e4 / 1e4}"
        return f"({start}) -> ({finish})"

    @staticmethod
    def pack_geometry(coordinates: list[list[float]]) -> bytes:
        return zlib.compress(json.dumps(coordinates, separators=(",", ":")).encode(), level=6)

    @property
    def coordinates(self) -> list[list[float]]:
        return json.loads(zlib.decompress(bytes(self.geometry_zlib))) if self.geometry_zlib else []

    @property
    def stored_kb(self) -> float:
        return len(self.geometry_zlib or b"") / 1024


class StoredGeocode(models.Model):
    """Result of geocoding one free-text address ("City, ST" and coordinates never need this)."""

    query = models.CharField(max_length=255, unique=True)  # normalised: lower case, single spaces
    status = models.CharField(max_length=16, choices=LookupStatus.choices, default=LookupStatus.OK)
    lat = models.FloatField(null=True, blank=True)
    lon = models.FloatField(null=True, blank=True)
    label = models.CharField(max_length=255, blank=True)
    fetched_at = models.DateTimeField()
    last_used_at = models.DateTimeField()
    hit_count = models.PositiveIntegerField(default=0)

    def __str__(self) -> str:
        return self.query
