"""Read and write locally stored routing results.

Rules:
* Successful routes/geocodes are reused for ROUTE_STORE_MAX_AGE_DAYS.
* "No route" / "address not found" answers are reused for NEGATIVE_STORE_MAX_AGE_DAYS,
  so a hopeless request doesn't call the API every time.
* Transport errors, rate limits, 5xx and key problems are never stored.
"""

import re
from datetime import timedelta

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from routing.clients.ors import GeocodeResult, RouteResult
from routing.models import LookupStatus, StoredGeocode, StoredRoute


def _e4(value: float) -> int:
    return round(value * 10_000)


def route_key(start: tuple[float, float], finish: tuple[float, float]) -> dict:
    return {
        "start_lat_e4": _e4(start[0]),
        "start_lon_e4": _e4(start[1]),
        "finish_lat_e4": _e4(finish[0]),
        "finish_lon_e4": _e4(finish[1]),
    }


def normalise_query(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def is_fresh(row: StoredRoute | StoredGeocode) -> bool:
    days = settings.ROUTE_STORE_MAX_AGE_DAYS if row.status == LookupStatus.OK else settings.NEGATIVE_STORE_MAX_AGE_DAYS
    return timezone.now() - row.fetched_at < timedelta(days=days)


def mark_used(row: StoredRoute | StoredGeocode) -> None:
    type(row).objects.filter(pk=row.pk).update(hit_count=F("hit_count") + 1, last_used_at=timezone.now())


# --- routes -----------------------------------------------------------------


def get_route(start: tuple[float, float], finish: tuple[float, float]) -> StoredRoute | None:
    return StoredRoute.objects.filter(**route_key(start, finish)).first()


def route_from_row(row: StoredRoute) -> RouteResult:
    return RouteResult(
        coordinates=row.coordinates,
        distance_miles=row.distance_miles,
        duration_hours=row.duration_hours,
        profile=row.profile,
    )


def save_route(start: tuple[float, float], finish: tuple[float, float], route: RouteResult) -> StoredRoute:
    now = timezone.now()
    row, _ = StoredRoute.objects.update_or_create(
        **route_key(start, finish),
        defaults={
            "status": LookupStatus.OK,
            "profile": route.profile,
            "distance_miles": route.distance_miles,
            "duration_hours": route.duration_hours,
            "geometry_zlib": StoredRoute.pack_geometry(route.coordinates),
            "error_message": "",
            "fetched_at": now,
            "last_used_at": now,
        },
    )
    return row


def save_route_not_found(start: tuple[float, float], finish: tuple[float, float], message: str) -> StoredRoute:
    now = timezone.now()
    row, _ = StoredRoute.objects.update_or_create(
        **route_key(start, finish),
        defaults={
            "status": LookupStatus.NOT_FOUND,
            "profile": "",
            "distance_miles": None,
            "duration_hours": None,
            "geometry_zlib": None,
            "error_message": message,
            "fetched_at": now,
            "last_used_at": now,
        },
    )
    return row


# --- geocodes ---------------------------------------------------------------


def get_geocode(text: str) -> StoredGeocode | None:
    return StoredGeocode.objects.filter(query=normalise_query(text)).first()


def save_geocode(text: str, result: GeocodeResult | None) -> StoredGeocode:
    now = timezone.now()
    found = result is not None
    row, _ = StoredGeocode.objects.update_or_create(
        query=normalise_query(text),
        defaults={
            "status": LookupStatus.OK if found else LookupStatus.NOT_FOUND,
            "lat": result.lat if found else None,
            "lon": result.lon if found else None,
            "label": result.label if found else "",
            "fetched_at": now,
            "last_used_at": now,
        },
    )
    return row
