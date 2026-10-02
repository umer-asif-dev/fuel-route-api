import random

import pytest

from routing.services.optimizer import InfeasibleRouteError, StationOnRoute, plan_fuel_stops


def s(station_id, mile, price):
    return StationOnRoute(station_id=station_id, mile=mile, price=price)


def test_short_trip_buys_only_what_it_needs_at_the_start():
    plan = plan_fuel_stops(120, [s(1, 0, 3.00), s(2, 60, 2.50)], range_miles=500, mpg=10)
    # Station 2 is cheaper and in reach, so buy 6 gal at station 1, then 6 gal at station 2.
    assert [(st.station_id, round(st.gallons, 6)) for st in plan.stops] == [(1, 6), (2, 6)]
    assert plan.total_cost == pytest.approx(6 * 3.00 + 6 * 2.50)
    assert plan.total_gallons == pytest.approx(12)


def test_fills_up_when_nothing_cheaper_is_in_reach():
    stations = [s(1, 0, 2.00), s(2, 400, 4.00), s(3, 700, 3.00)]
    plan = plan_fuel_stops(900, stations, range_miles=500, mpg=10)
    # Fill 50 gal at $2 (cheapest), move to cheapest in reach (station 2 at mile 400),
    # top up only enough to reach the cheaper station 3, then finish from there.
    assert [(st.station_id, round(st.gallons, 6)) for st in plan.stops] == [(1, 50), (2, 20), (3, 20)]
    assert plan.total_cost == pytest.approx(50 * 2 + 20 * 4 + 20 * 3)
    assert plan.total_gallons == pytest.approx(90)


def test_skips_expensive_stations_it_can_pass():
    stations = [s(1, 0, 3.00), s(2, 100, 5.00), s(3, 300, 2.80)]
    plan = plan_fuel_stops(450, stations)
    assert [st.station_id for st in plan.stops] == [1, 3]
    assert plan.total_cost == pytest.approx(30 * 3.00 + 15 * 2.80)


def test_total_gallons_always_cover_the_whole_trip_when_starting_empty():
    stations = [s(i, m, p) for i, (m, p) in enumerate([(0, 3.5), (210, 3.1), (480, 3.9), (690, 2.9), (1100, 3.3)])]
    plan = plan_fuel_stops(1400, stations)
    assert plan.total_gallons == pytest.approx(140)


def test_gap_longer_than_range_is_infeasible():
    with pytest.raises(InfeasibleRouteError) as exc:
        plan_fuel_stops(1200, [s(1, 0, 3.0), s(2, 450, 3.0), s(3, 1000, 3.0)])
    assert exc.value.from_mile == 450
    assert exc.value.to_mile == 1000


def test_starting_empty_needs_a_station_at_the_origin():
    with pytest.raises(InfeasibleRouteError):
        plan_fuel_stops(300, [s(1, 20, 3.0)])


def test_start_fuel_can_cover_the_whole_trip():
    plan = plan_fuel_stops(200, [s(1, 50, 3.0)], start_fuel_gallons=25)
    assert plan.stops == []
    assert plan.total_cost == 0


def test_start_fuel_reaches_first_station():
    plan = plan_fuel_stops(300, [s(1, 50, 3.0)], start_fuel_gallons=10)
    assert [(st.station_id, round(st.gallons, 6)) for st in plan.stops] == [(1, 20)]
    assert plan.stops[0].fuel_before_gallons == pytest.approx(5)


def test_stations_past_the_destination_are_ignored():
    plan = plan_fuel_stops(100, [s(1, 0, 3.0), s(2, 150, 1.0)])
    assert [st.station_id for st in plan.stops] == [1]


def _brute_force_cost(total, stations, capacity):
    """Exact DP over integer fuel levels; stations and distances are integers."""
    stations = sorted(stations, key=lambda st: st.mile)
    inf = float("inf")
    # best[f] = min cost to be at the current station holding f units of fuel
    best = {0: 0.0}
    for k, st in enumerate(stations):
        # Buy any amount here.
        bought = {}
        for f, cost in best.items():
            for extra in range(capacity - f + 1):
                c = cost + extra * st.price
                if c < bought.get(f + extra, inf):
                    bought[f + extra] = c
        next_mile = stations[k + 1].mile if k + 1 < len(stations) else total
        dist = next_mile - st.mile
        best = {f - dist: c for f, c in bought.items() if f >= dist}
        if not best:
            return None
    return min(best.values())


@pytest.mark.parametrize("seed", range(200))
def test_greedy_matches_exhaustive_search(seed):
    rng = random.Random(seed)
    capacity = rng.randint(5, 15)
    total = rng.randint(1, 60)
    miles = sorted({0} | {rng.randint(1, total) for _ in range(rng.randint(0, 10))})
    stations = [s(i, m, rng.choice([2.5, 2.9, 3.0, 3.3, 3.7, 4.1])) for i, m in enumerate(miles)]

    expected = _brute_force_cost(total, stations, capacity)
    if expected is None:
        with pytest.raises(InfeasibleRouteError):
            plan_fuel_stops(total, stations, range_miles=capacity, mpg=1)
    else:
        plan = plan_fuel_stops(total, stations, range_miles=capacity, mpg=1)
        assert plan.total_cost == pytest.approx(expected)
        assert plan.total_gallons == pytest.approx(total)


def test_zero_length_trip_costs_nothing():
    plan = plan_fuel_stops(0, [s(1, 0, 3.0)])
    assert plan.stops == [] and plan.total_cost == 0


def test_station_exactly_at_range_limit_is_reachable():
    plan = plan_fuel_stops(800, [s(1, 0, 3.0), s(2, 500, 3.0)])
    assert [(st.station_id, round(st.gallons, 6)) for st in plan.stops] == [(1, 50), (2, 30)]


def test_gap_just_over_range_is_infeasible():
    with pytest.raises(InfeasibleRouteError):
        plan_fuel_stops(800, [s(1, 0, 3.0), s(2, 500.5, 3.0)])


def test_input_order_and_same_mile_stations_do_not_matter():
    stations = [s(3, 300, 3.1), s(1, 0, 3.5), s(2, 0, 3.2), s(4, 300, 2.9)]
    plan = plan_fuel_stops(600, stations)
    # Buy at the cheaper of the two origin stations, then the cheaper one at mile 300.
    assert [st.station_id for st in plan.stops] == [2, 4]
    assert plan.total_cost == pytest.approx(30 * 3.2 + 30 * 2.9)


def test_start_fuel_above_capacity_is_capped():
    plan = plan_fuel_stops(600, [s(1, 400, 3.0)], start_fuel_gallons=80)
    assert plan.stops[0].fuel_before_gallons == pytest.approx(10)
    assert plan.total_gallons == pytest.approx(10)


def test_rejects_bad_vehicle_parameters():
    with pytest.raises(ValueError):
        plan_fuel_stops(100, [s(1, 0, 3.0)], mpg=0)


def _linear_program_cost(total, stations, range_miles, mpg, start_fuel_miles=0.0):
    """Exact optimum via LP: x_k = miles of fuel bought at station k (sorted by mile)."""
    import numpy as np
    from scipy.optimize import linprog

    st = sorted(stations, key=lambda x: x.mile)
    n = len(st)
    a_ub, b_ub = [], []
    for k in range(n):
        before = np.array([1.0 if j < k else 0.0 for j in range(n)])
        through = np.array([1.0 if j <= k else 0.0 for j in range(n)])
        a_ub.append(-before)  # fuel on arrival at k >= 0
        b_ub.append(start_fuel_miles - st[k].mile)
        a_ub.append(through)  # fuel after buying at k <= tank size
        b_ub.append(range_miles - start_fuel_miles + st[k].mile)
    a_ub.append(-np.ones(n))  # enough fuel to reach the destination
    b_ub.append(start_fuel_miles - total)
    result = linprog([x.price / mpg for x in st], A_ub=a_ub, b_ub=b_ub, bounds=[(0, None)] * n)
    return result.fun if result.status == 0 else None


@pytest.mark.parametrize("seed", range(150))
def test_greedy_matches_linear_program_on_realistic_routes(seed):
    rng = random.Random(1000 + seed)
    total = rng.uniform(50, 2800)
    miles = [0.0] + [rng.uniform(0, total) for _ in range(rng.randint(3, 60))]
    stations = [s(i, m, round(rng.uniform(2.7, 4.6), 3)) for i, m in enumerate(miles)]
    start_fuel = rng.choice([0, 0, 0, rng.uniform(0, 50)])

    expected = _linear_program_cost(total, stations, 500, 10, start_fuel * 10)
    if expected is None:
        with pytest.raises(InfeasibleRouteError):
            plan_fuel_stops(total, stations, start_fuel_gallons=start_fuel)
        return
    plan = plan_fuel_stops(total, stations, start_fuel_gallons=start_fuel)
    assert plan.total_cost == pytest.approx(expected, rel=1e-7, abs=1e-6)
    # Never exceeds the tank, never runs dry between stops.
    fuel_miles = min(start_fuel * 10, 500)
    position = 0.0
    for stop in plan.stops:
        fuel_miles -= stop.mile - position
        assert fuel_miles >= -1e-6
        assert stop.fuel_before_gallons * 10 == pytest.approx(fuel_miles, abs=1e-6)
        fuel_miles += stop.gallons * 10
        assert fuel_miles <= 500 + 1e-6
        position = stop.mile
    assert fuel_miles - (total - position) >= -1e-6


def test_small_top_ups_are_folded_into_the_previous_stop():
    # Station 2 is a hair cheaper than station 1 and station 3 cheaper still, so the
    # pure optimum buys 0.2 gal at station 2 just to reach station 3.
    stations = [s(1, 0, 3.06), s(2, 201, 3.02), s(3, 203, 2.98)]
    pure = plan_fuel_stops(600, stations)
    assert [st.station_id for st in pure.stops] == [1, 2, 3]

    tidy = plan_fuel_stops(600, stations, min_stop_gallons=5)
    assert [st.station_id for st in tidy.stops] == [1, 3]
    assert tidy.stops[0].gallons == pytest.approx(20.3)
    assert tidy.total_gallons == pytest.approx(pure.total_gallons)
    assert tidy.total_cost - pure.total_cost == pytest.approx(0.2 * (3.06 - 3.02))


def test_small_top_up_after_a_full_tank_moves_to_the_next_stop():
    # Mirrors Waco -> Overton -> Lexington, NE: fill up at the cheapest station, move to the
    # cheapest in reach (Overton), fill again, then a 1.3 gal top-up 13 miles later.
    stations = [s(1, 0, 2.80), s(2, 110, 2.90), s(3, 123, 2.98), s(4, 233, 3.01), s(5, 600, 3.30)]
    pure = plan_fuel_stops(900, stations)
    assert [(st.station_id, round(st.gallons, 2)) for st in pure.stops][:3] == [(1, 50), (2, 11), (3, 1.3)]

    tidy = plan_fuel_stops(900, stations, min_stop_gallons=5)
    assert 3 not in [st.station_id for st in tidy.stops]
    assert tidy.total_gallons == pytest.approx(pure.total_gallons)
    assert 0 <= tidy.total_cost - pure.total_cost < 0.1


def test_small_top_up_is_kept_when_it_cannot_move():
    # Tank full before it and the next stop is reached empty: the top-up is required.
    stations = [s(1, 0, 3.5), s(2, 500, 3.4), s(3, 502, 3.0)]
    plan = plan_fuel_stops(900, stations, min_stop_gallons=5)
    assert [st.station_id for st in plan.stops] == [1, 2, 3]


def test_equal_prices_do_not_add_stops():
    plan = plan_fuel_stops(300, [s(1, 0, 3.079), s(2, 15, 3.079), s(3, 60, 3.049)])
    assert [st.station_id for st in plan.stops] == [1, 3]


@pytest.mark.parametrize("seed", range(100))
def test_merging_keeps_plans_feasible(seed):
    rng = random.Random(5000 + seed)
    total = rng.uniform(100, 2500)
    stations = [s(0, 0.0, 3.4)] + [s(i, rng.uniform(0, total), round(rng.uniform(2.7, 4.2), 3)) for i in range(1, 50)]
    try:
        plan = plan_fuel_stops(total, stations, min_stop_gallons=5)
    except InfeasibleRouteError:
        return
    fuel, position = 0.0, 0.0
    for stop in plan.stops:
        fuel -= stop.mile - position
        assert fuel >= -1e-6
        fuel += stop.gallons * 10
        assert fuel <= 500 + 1e-6
        position = stop.mile
    assert fuel >= total - position - 1e-6
    assert plan.total_gallons == pytest.approx(total / 10)
