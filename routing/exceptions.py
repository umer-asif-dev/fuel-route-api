"""Errors the API turns into specific HTTP responses."""


class RoutePlanningError(Exception):
    status_code = 500
    code = "error"

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details


class LocationNotFound(RoutePlanningError):
    status_code = 400
    code = "location_not_found"


class LocationOutsideUSA(RoutePlanningError):
    status_code = 400
    code = "location_outside_usa"


class RouteNotFound(RoutePlanningError):
    status_code = 422
    code = "route_not_found"


class NoFuelPlan(RoutePlanningError):
    status_code = 422
    code = "no_fuel_plan"


class RoutingServiceError(RoutePlanningError):
    status_code = 502
    code = "routing_service_error"


class RoutingNotConfigured(RoutePlanningError):
    status_code = 503
    code = "routing_not_configured"
