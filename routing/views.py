import logging
from urllib.parse import urlencode

from django.shortcuts import render
from django.urls import reverse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from routing.exceptions import RoutePlanningError
from routing.serializers import ErrorSerializer, RouteRequestSerializer
from routing.services.planner import RoutePlanner

logger = logging.getLogger(__name__)


_EXAMPLE_RESPONSE = {
    "start": {
        "query": "Chicago, IL",
        "label": "Chicago, IL",
        "lat": 41.837045,
        "lon": -87.684939,
        "resolved_by": "gazetteer",
    },
    "finish": {
        "query": "Denver, CO",
        "label": "Denver, CO",
        "lat": 39.76185,
        "lon": -104.881105,
        "resolved_by": "gazetteer",
    },
    "route": {"distance_miles": 1001.1, "duration_hours": 22.9, "profile": "driving-hgv"},
    "fuel": {
        "total_cost": 291.16,
        "total_gallons": 100.11,
        "average_price_per_gallon": 2.908,
        "stop_count": 5,
        "stops": [
            {
                "order": 1,
                "station": {
                    "opis_id": 70180,
                    "name": "Gulf",
                    "address": "SR-83",
                    "city": "Bensenville",
                    "state": "IL",
                    "lat": 41.958011,
                    "lon": -87.943825,
                },
                "mile_marker": 14.6,
                "off_route_miles": 6.1,
                "price_per_gallon": 3.059,
                "gallons": 20.21,
                "cost": 61.84,
                "fuel_on_arrival_gallons": 0.0,
            }
        ],
    },
    "assumptions": {
        "vehicle_range_miles": 500,
        "mpg": 10,
        "starts_with_empty_tank": True,
        "corridor_miles_requested": 10.0,
        "corridor_miles_used": 10.0,
    },
    "geojson": {"type": "FeatureCollection", "features": ["route LineString, start/finish Points, fuel_stop Points"]},
    "meta": {
        "external_calls": 0,
        "route_source": "stored",
        "route_fetched_at": "2026-10-01T11:45:21+00:00",
        "stations_in_corridor": 202,
        "timings_ms": {"compute": 9.0, "total": 21.5},
    },
    "map_url": "http://localhost:8000/api/v1/route/map/?start=Chicago%2C+IL&finish=Denver%2C+CO",
}

_ERROR_RESPONSES = {
    400: OpenApiResponse(ErrorSerializer, description="Invalid parameters, unknown place, or a place outside the USA."),
    422: OpenApiResponse(ErrorSerializer, description="No drivable route, or no fuel plan (station gap > 500 mi)."),
    502: OpenApiResponse(ErrorSerializer, description="The routing provider failed or timed out."),
    503: OpenApiResponse(ErrorSerializer, description="ORS_API_KEY missing or rejected."),
}
_ROUTE_DOC = {
    "summary": "Plan the cheapest fuel stops between two US locations",
    "description": (
        "Locations may be `City, ST` or `lat,lon` (resolved offline, no API call) or a street address "
        "(geocoded once, then stored). The route is fetched from OpenRouteService at most once per trip and "
        "stored locally; `meta.external_calls` and `meta.route_source` show what happened."
    ),
    "responses": {
        200: OpenApiResponse(
            OpenApiTypes.OBJECT,
            examples=[OpenApiExample("Chicago to Denver", value=_EXAMPLE_RESPONSE, response_only=True)],
        ),
        **_ERROR_RESPONSES,
    },
    "tags": ["Route"],
}


def error_body(code: str, message: str, **details) -> dict:
    return {"error": {"code": code, "message": message, **({"details": details} if details else {})}}


class RouteView(APIView):
    """Cheapest fuel stops between two US locations.

    GET  /api/v1/route/?start=Chicago, IL&finish=Denver, CO[&corridor_miles=10]
    POST /api/v1/route/  {"start": "...", "finish": "...", "corridor_miles": 10}
    """

    @extend_schema(
        parameters=[
            OpenApiParameter("start", str, required=True, description='e.g. "Chicago, IL" or "41.88,-87.63"'),
            OpenApiParameter("finish", str, required=True, description='e.g. "Denver, CO"'),
            OpenApiParameter("corridor_miles", float, description="Max distance of a station from the route (1-50)."),
        ],
        **_ROUTE_DOC,
    )
    def get(self, request):
        return self._handle(request, request.query_params)

    @extend_schema(request=RouteRequestSerializer, **_ROUTE_DOC)
    def post(self, request):
        return self._handle(request, request.data)

    def _handle(self, request, data):
        serializer = RouteRequestSerializer(data=data)
        if not serializer.is_valid():
            return Response(
                error_body("invalid_request", "Check the request parameters.", fields=serializer.errors),
                status=status.HTTP_400_BAD_REQUEST,
            )
        params = serializer.validated_data
        try:
            body = RoutePlanner().plan(params["start"], params["finish"], params.get("corridor_miles"))
        except RoutePlanningError as exc:
            logger.info("Route request failed: %s (%s)", exc.code, exc.message)
            return Response(error_body(exc.code, exc.message, **exc.details), status=exc.status_code)

        query = {"start": params["start"], "finish": params["finish"]}
        if "corridor_miles" in params:
            query["corridor_miles"] = params["corridor_miles"]
        body["map_url"] = request.build_absolute_uri(f"{reverse('route-map')}?{urlencode(query)}")
        logger.info(
            "Route %s -> %s: %s stops, $%s, %s external calls, %.0f ms",
            params["start"],
            params["finish"],
            body["fuel"]["stop_count"],
            body["fuel"]["total_cost"],
            body["meta"]["external_calls"],
            body["meta"]["timings_ms"]["total"],
        )
        return Response(body)


def route_map(request):
    """Leaflet map of the route and fuel stops. Reuses cached routing, so it adds no API calls."""
    serializer = RouteRequestSerializer(data=request.GET)
    if not serializer.is_valid():
        return render(
            request,
            "routing/map.html",
            {"error": "Pass start and finish, e.g. ?start=Chicago, IL&finish=Denver, CO"},
            status=400,
        )
    params = serializer.validated_data
    try:
        body = RoutePlanner().plan(params["start"], params["finish"], params.get("corridor_miles"))
    except RoutePlanningError as exc:
        return render(request, "routing/map.html", {"error": exc.message}, status=exc.status_code)
    return render(request, "routing/map.html", {"plan": body})
