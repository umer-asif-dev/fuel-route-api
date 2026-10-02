"""Orchestrates a route request: resolve inputs, fetch one route, match stations, optimise.

Routes and geocodes are stored in the database (see route_store.py), so each trip
or address costs at most one API call; repeat requests, restarts and requests that
only change the corridor width cost none.
"""

import time
from dataclasses import dataclass, field

import numpy as np
from django.conf import settings

from routing.clients.ors import ORSClient, RouteResult
from routing.exceptions import LocationNotFound, NoFuelPlan, RouteNotFound, RoutingNotConfigured, RoutingServiceError
from routing.services import route_store
from routing.services.locations import ResolvedLocation, resolve_location
from routing.services.optimizer import InfeasibleRouteError, StationOnRoute, plan_fuel_stops
from routing.services.route_geometry import RouteLine, match_stations_to_route, simplify
from stations.services.index import StationIndex, get_station_index

# Wider corridors tried, in order, when the requested one leaves a gap longer than the range.
FALLBACK_CORRIDORS_MILES = (25.0, 50.0)

# Failures that say nothing about the route itself: never stored, and an expired stored
# copy is served instead when one exists.
_TRANSIENT_ERRORS = (RoutingServiceError, RoutingNotConfigured)


@dataclass
class RequestStats:
    external_calls: int = 0
    route_source: str = ""  # "api" | "stored" | "stored_stale"
    route_fetched_at: str = ""
    geocode_sources: list[str] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)


class RoutePlanner:
    def __init__(self, station_index: StationIndex | None = None, client: ORSClient | None = None):
        self._station_index = station_index
        self._client = client

    @property
    def client(self) -> ORSClient:
        # Built lazily: requests answered from stored data don't need an API key at all.
        if self._client is None:
            self._client = ORSClient(settings.ORS_API_KEY, settings.ORS_BASE_URL, settings.ORS_TIMEOUT_S)
        return self._client

    @property
    def stations(self) -> StationIndex:
        if self._station_index is None:
            self._station_index = get_station_index()
        return self._station_index

    def plan(self, start: str, finish: str, corridor_miles: float | None = None) -> dict:
        started = time.perf_counter()
        stats = RequestStats()
        corridor = float(corridor_miles or settings.CORRIDOR_MILES)

        origin = self._resolve(start, stats)
        destination = self._resolve(finish, stats)
        route = self._route(origin, destination, stats)

        t = time.perf_counter()
        line = RouteLine.from_lonlat(route.coordinates, scale_to_miles=route.distance_miles)
        plan, matches, corridor_used = self._optimise(line, corridor)
        stats.timings_ms["compute"] = (time.perf_counter() - t) * 1000

        body = self._response(origin, destination, route, plan, matches, corridor, corridor_used)
        stats.timings_ms["total"] = (time.perf_counter() - started) * 1000
        body["meta"] = {
            "external_calls": stats.external_calls,
            "route_source": stats.route_source,
            "route_fetched_at": stats.route_fetched_at,
            **({"geocode_sources": stats.geocode_sources} if stats.geocode_sources else {}),
            "stations_in_corridor": int(len(matches.index)),
            "timings_ms": {k: round(v, 1) for k, v in stats.timings_ms.items()},
        }
        return body

    # --- inputs and routing -------------------------------------------------

    def _resolve(self, text: str, stats: RequestStats) -> ResolvedLocation:
        def geocode(query: str) -> tuple[float, float, str]:
            stored = route_store.get_geocode(query)
            if stored is not None and route_store.is_fresh(stored):
                return self._use_stored_geocode(stored, query, stats, "stored")

            before = None
            t = time.perf_counter()
            try:
                client = self.client
                before = client.calls_made
                result = client.geocode(query)
            except LocationNotFound:
                route_store.save_geocode(query, None)
                raise
            except _TRANSIENT_ERRORS:
                if stored is not None:
                    return self._use_stored_geocode(stored, query, stats, "stored_stale")
                raise
            finally:
                if before is not None:
                    stats.external_calls += self.client.calls_made - before
                stats.timings_ms["geocode"] = stats.timings_ms.get("geocode", 0) + (time.perf_counter() - t) * 1000
            route_store.save_geocode(query, result)
            stats.geocode_sources.append("api")
            return result.lat, result.lon, result.label

        return resolve_location(text, geocode)

    @staticmethod
    def _use_stored_geocode(stored, query: str, stats: RequestStats, source: str) -> tuple[float, float, str]:
        route_store.mark_used(stored)
        stats.geocode_sources.append(source)
        if stored.status != "ok":
            raise LocationNotFound(f"Could not find a US location matching '{query}'.", query=query)
        return stored.lat, stored.lon, stored.label

    def _route(self, origin: ResolvedLocation, destination: ResolvedLocation, stats: RequestStats) -> RouteResult:
        start, finish = (origin.lat, origin.lon), (destination.lat, destination.lon)
        stored = route_store.get_route(start, finish)
        if stored is not None and route_store.is_fresh(stored):
            return self._use_stored_route(stored, stats, "stored")

        t = time.perf_counter()
        before = None
        try:
            route = None
            profiles = settings.ORS_PROFILES
            for i, profile in enumerate(profiles):
                try:
                    client = self.client
                    before = client.calls_made if before is None else before
                    route = client.directions(start, finish, profile)
                    break
                except RouteNotFound as exc:
                    # Some roads are closed to trucks; fall back to a car route.
                    if i == len(profiles) - 1:
                        route_store.save_route_not_found(start, finish, exc.message)
                        raise
        except _TRANSIENT_ERRORS:
            # The provider is down or the key is bad: an old answer beats no answer.
            if stored is not None and stored.status == "ok":
                return self._use_stored_route(stored, stats, "stored_stale")
            raise
        finally:
            if before is not None:
                stats.external_calls += self.client.calls_made - before
            stats.timings_ms["routing"] = (time.perf_counter() - t) * 1000

        row = route_store.save_route(start, finish, route)
        stats.route_source = "api"
        stats.route_fetched_at = row.fetched_at.isoformat()
        return route

    @staticmethod
    def _use_stored_route(stored, stats: RequestStats, source: str) -> RouteResult:
        route_store.mark_used(stored)
        stats.route_source = source
        stats.route_fetched_at = stored.fetched_at.isoformat()
        if stored.status != "ok":
            raise RouteNotFound(stored.error_message or "No drivable route between these locations.")
        return route_store.route_from_row(stored)

    # --- fuel planning ------------------------------------------------------

    def _optimise(self, line: RouteLine, corridor: float):
        """Plan with the requested corridor, widening it only if a gap makes the trip impossible."""
        corridors = [corridor] + [c for c in FALLBACK_CORRIDORS_MILES if c > corridor]
        last_error = None
        for width in corridors:
            matches = match_stations_to_route(line, self.stations.lat, self.stations.lon, width)
            # Starting empty, the truck fuels up near the origin before setting off.
            optimiser_miles = np.where(matches.mile <= settings.ORIGIN_RADIUS_MILES, 0.0, matches.mile)
            candidates = [
                StationOnRoute(station_id=k, mile=float(m), price=float(self.stations.price[i]))
                for k, (i, m) in enumerate(zip(matches.index, optimiser_miles, strict=True))
            ]
            try:
                plan = plan_fuel_stops(
                    line.length_miles,
                    candidates,
                    range_miles=settings.VEHICLE_RANGE_MILES,
                    mpg=settings.VEHICLE_MPG,
                    min_stop_gallons=settings.MIN_STOP_GALLONS,
                )
                return plan, matches, width
            except InfeasibleRouteError as exc:
                last_error = exc

        if last_error.from_mile == 0 and not any(m <= settings.ORIGIN_RADIUS_MILES for m in matches.mile):
            raise NoFuelPlan(
                f"No fuel station within {settings.ORIGIN_RADIUS_MILES:.0f} miles of the start; "
                "the vehicle starts with an empty tank.",
            )
        raise NoFuelPlan(
            f"No fuel station between mile {last_error.from_mile:.0f} and mile {last_error.to_mile:.0f} "
            f"of the route, which is more than the {settings.VEHICLE_RANGE_MILES}-mile range.",
            gap_from_mile=round(last_error.from_mile, 1),
            gap_to_mile=round(last_error.to_mile, 1),
        )

    # --- response -----------------------------------------------------------

    def _response(self, origin, destination, route, plan, matches, corridor_requested, corridor_used) -> dict:
        stops = []
        for order, stop in enumerate(plan.stops, start=1):
            index = int(matches.index[stop.station_id])
            info = self.stations.stations[index]
            stops.append(
                {
                    "order": order,
                    "station": {
                        "opis_id": info.opis_id,
                        "name": info.name,
                        "address": info.address,
                        "city": info.city,
                        "state": info.state,
                        "lat": info.lat,
                        "lon": info.lon,
                    },
                    "mile_marker": round(float(matches.mile[stop.station_id]), 1),
                    "off_route_miles": round(float(matches.off_route_miles[stop.station_id]), 1),
                    "price_per_gallon": round(stop.price, 3),
                    "gallons": round(stop.gallons, 2),
                    "cost": round(stop.cost, 2),
                    "fuel_on_arrival_gallons": round(stop.fuel_before_gallons, 2),
                }
            )

        coords = np.asarray(route.coordinates, dtype=float)
        keep = simplify(coords[:, 1], coords[:, 0], settings.MAP_SIMPLIFY_TOLERANCE_MILES)
        line_coords = [[round(float(x), 5), round(float(y), 5)] for x, y in coords[keep]]

        return {
            "start": origin.as_dict(),
            "finish": destination.as_dict(),
            "route": {
                "distance_miles": round(route.distance_miles, 1),
                "duration_hours": round(route.duration_hours, 2),
                "profile": route.profile,
            },
            "fuel": {
                "total_cost": round(plan.total_cost, 2),
                "total_gallons": round(plan.total_gallons, 2),
                "average_price_per_gallon": round(plan.total_cost / plan.total_gallons, 3)
                if plan.total_gallons
                else None,
                "stop_count": len(stops),
                "stops": stops,
            },
            "assumptions": {
                "vehicle_range_miles": settings.VEHICLE_RANGE_MILES,
                "mpg": settings.VEHICLE_MPG,
                "starts_with_empty_tank": True,
                "corridor_miles_requested": corridor_requested,
                "corridor_miles_used": corridor_used,
            },
            "geojson": _feature_collection(origin, destination, line_coords, route, stops),
        }


def _feature_collection(origin, destination, line_coords, route, stops) -> dict:
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": line_coords},
            "properties": {"kind": "route", "distance_miles": round(route.distance_miles, 1)},
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [origin.lon, origin.lat]},
            "properties": {"kind": "start", "label": origin.label},
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [destination.lon, destination.lat]},
            "properties": {"kind": "finish", "label": destination.label},
        },
    ]
    for stop in stops:
        st = stop["station"]
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [st["lon"], st["lat"]]},
                "properties": {
                    "kind": "fuel_stop",
                    "order": stop["order"],
                    "name": st["name"],
                    "city": st["city"],
                    "state": st["state"],
                    "mile_marker": stop["mile_marker"],
                    "price_per_gallon": stop["price_per_gallon"],
                    "gallons": stop["gallons"],
                    "cost": stop["cost"],
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}
