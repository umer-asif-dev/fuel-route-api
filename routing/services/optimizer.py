"""Choose where to refuel along a route so the total fuel bill is as low as possible.

This is the fixed-capacity "gas station problem". With fuel bought in any
amount and stations visited in route order, the following greedy is optimal:

At each station, look ahead as far as a full tank reaches.
  * If a cheaper station is in reach, buy only enough to get there.
  * Otherwise, fill the tank (or buy just enough to finish the trip) and move
    to the cheapest station in reach.

Fuel is tracked in miles of range internally and converted to gallons for output.
The module has no Django dependency so it can be unit-tested in isolation.
"""

from dataclasses import dataclass, field

_EPS = 1e-9


@dataclass(frozen=True)
class StationOnRoute:
    """A station projected onto the route.

    ``mile`` is the distance along the route; ``station_id`` is any identifier
    the caller wants back.
    """

    station_id: int
    mile: float
    price: float


@dataclass(frozen=True)
class FuelStop:
    station_id: int
    mile: float
    price: float
    gallons: float
    cost: float
    fuel_before_gallons: float


@dataclass
class FuelPlan:
    total_miles: float
    total_gallons: float
    total_cost: float
    stops: list[FuelStop] = field(default_factory=list)


class InfeasibleRouteError(Exception):
    """No station sequence lets the vehicle cover the route on its range."""

    def __init__(self, from_mile: float, to_mile: float, range_miles: float):
        self.from_mile = from_mile
        self.to_mile = to_mile
        self.range_miles = range_miles
        super().__init__(
            f"No fuel station reachable between mile {from_mile:.1f} and mile {to_mile:.1f} "
            f"(vehicle range is {range_miles:.0f} miles)."
        )


def plan_fuel_stops(
    total_miles: float,
    stations: list[StationOnRoute],
    *,
    range_miles: float = 500,
    mpg: float = 10,
    start_fuel_gallons: float = 0,
    min_stop_gallons: float = 0,
) -> FuelPlan:
    """Return the cheapest refuelling plan for a trip of ``total_miles``.

    ``stations`` may be in any order; stations beyond the destination are
    ignored. With ``start_fuel_gallons=0`` the vehicle starts empty, so the
    caller must supply a station at mile 0 (e.g. one near the origin).
    Raises ``InfeasibleRouteError`` when a gap between stations exceeds the range.

    ``min_stop_gallons`` folds tiny purchases into the previous stop when the
    tank has room, trading a few cents for a plan a driver would actually follow.
    """
    if total_miles < 0 or range_miles <= 0 or mpg <= 0:
        raise ValueError("total_miles must be >= 0 and range_miles/mpg must be > 0")

    fuel = min(start_fuel_gallons * mpg, range_miles)  # miles of range in the tank
    route = sorted((s for s in stations if -_EPS <= s.mile <= total_miles + _EPS), key=lambda s: s.mile)
    stops: list[FuelStop] = []

    position = 0.0
    i = 0  # index of the next station not yet reached

    # Before the first purchase the vehicle can only use the fuel it starts with.
    # Stopping at a station costs nothing, so drive to the nearest one in reach.
    if total_miles - position <= fuel + _EPS:
        return _finish(total_miles, mpg, stops)
    if not route or route[0].mile - position > fuel + _EPS:
        next_mile = route[0].mile if route else total_miles
        raise InfeasibleRouteError(position, next_mile, range_miles)
    fuel -= route[0].mile - position

    while True:
        here = route[i]
        reach = here.mile + range_miles

        # First strictly cheaper station within a full tank; the destination counts as free.
        # (Treating equal prices as "not cheaper" is equally optimal and avoids extra stops.)
        target_mile, target_index = None, None
        cheapest_index = None
        j = i + 1
        while j < len(route) and route[j].mile <= reach + _EPS:
            if route[j].price < here.price:
                target_mile, target_index = route[j].mile, j
                break
            if cheapest_index is None or route[j].price <= route[cheapest_index].price:
                cheapest_index = j  # ties go to the farther station
            j += 1
        if target_mile is None and total_miles <= reach + _EPS:
            target_mile, target_index = total_miles, None

        if target_mile is not None:
            # Buy just enough to reach a cheaper station or the destination.
            needed = max(0.0, (target_mile - here.mile) - fuel)
        else:
            if cheapest_index is None:
                next_mile = route[i + 1].mile if i + 1 < len(route) else total_miles
                raise InfeasibleRouteError(here.mile, next_mile, range_miles)
            # Nothing cheaper ahead: fill up and move to the cheapest station in reach.
            needed = range_miles - fuel
            target_mile, target_index = route[cheapest_index].mile, cheapest_index

        if needed > _EPS:
            gallons = needed / mpg
            stops.append(
                FuelStop(
                    station_id=here.station_id,
                    mile=here.mile,
                    price=here.price,
                    gallons=gallons,
                    cost=gallons * here.price,
                    fuel_before_gallons=fuel / mpg,
                )
            )
            fuel += needed

        fuel -= target_mile - here.mile
        if target_index is None:
            return _finish(total_miles, mpg, _merge_small_stops(stops, min_stop_gallons, range_miles / mpg))
        i = target_index


def _merge_small_stops(stops: list[FuelStop], min_gallons: float, capacity_gallons: float) -> list[FuelStop]:
    """Remove top-ups smaller than ``min_gallons`` by buying that fuel at a neighbouring stop.

    * Earlier: buy it at the previous stop, if the tank has room there. The tank
      level only rises between the two stops, so the trip stays feasible.
    * Later: skip it and buy it at the next stop, if the truck still arrives there
      with at least that much fuel left (so it never runs dry on the way).
    Each move costs ``gallons x price difference``, typically a few cents.
    """
    if min_gallons <= 0:
        return stops
    stops = list(stops)
    changed = True
    while changed:
        changed = False
        for k, stop in enumerate(stops):
            if stop.gallons >= min_gallons:
                continue
            prev = stops[k - 1] if k > 0 else None
            nxt = stops[k + 1] if k + 1 < len(stops) else None
            if prev is not None and prev.fuel_before_gallons + prev.gallons + stop.gallons <= capacity_gallons + _EPS:
                stops[k - 1] = _with_gallons(prev, prev.gallons + stop.gallons)
                # The next stop is reached with the same fuel as before.
                del stops[k]
            elif nxt is not None and nxt.fuel_before_gallons >= stop.gallons - _EPS:
                stops[k + 1] = _with_gallons(
                    nxt, nxt.gallons + stop.gallons, fuel_before=nxt.fuel_before_gallons - stop.gallons
                )
                del stops[k]
            else:
                continue
            changed = True
            break
    return stops


def _with_gallons(stop: FuelStop, gallons: float, fuel_before: float | None = None) -> FuelStop:
    return FuelStop(
        station_id=stop.station_id,
        mile=stop.mile,
        price=stop.price,
        gallons=gallons,
        cost=gallons * stop.price,
        fuel_before_gallons=stop.fuel_before_gallons if fuel_before is None else fuel_before,
    )


def _finish(total_miles: float, mpg: float, stops: list[FuelStop]) -> FuelPlan:
    return FuelPlan(
        total_miles=total_miles,
        total_gallons=sum(s.gallons for s in stops),
        total_cost=sum(s.cost for s in stops),
        stops=stops,
    )
