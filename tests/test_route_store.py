"""Stored OpenRouteService results: survive restarts, expire, and save API calls."""

import io
from datetime import timedelta

import pytest
import requests
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from routing.models import LookupStatus, StoredGeocode, StoredRoute
from stations.services.index import reset_station_index
from tests.conftest import FakeResponse, load_route_fixture

pytestmark = pytest.mark.django_db

CHI_DEN = {"start": "Chicago, IL", "finish": "Denver, CO"}
ADDRESS = "1437 Bannock St, Denver"


def get_route(client, **params):
    return client.get(reverse("route"), params)


def simulate_restart():
    """Drop every in-process state; only the database survives, as after a redeploy."""
    reset_station_index()


def backdate(model, days):
    model.objects.update(fetched_at=timezone.now() - timedelta(days=days))


def test_route_is_stored_and_survives_a_restart(client, fake_ors):
    first = get_route(client, **CHI_DEN).json()
    assert first["meta"]["external_calls"] == 1 and first["meta"]["route_source"] == "api"
    assert StoredRoute.objects.count() == 1

    simulate_restart()
    again = get_route(client, **CHI_DEN).json()
    assert again["meta"]["external_calls"] == 0
    assert again["meta"]["route_source"] == "stored"
    assert again["meta"]["route_fetched_at"] == first["meta"]["route_fetched_at"]
    assert again["fuel"] == first["fuel"] and again["route"] == first["route"]
    assert len(fake_ors.calls) == 1


def test_spellings_and_corridors_share_one_stored_row(client, fake_ors):
    get_route(client, **CHI_DEN)
    get_route(client, start="chicago illinois", finish="DENVER, colorado, USA")
    get_route(client, start="41.837045,-87.684939", finish="39.76185,-104.881105", corridor_miles=25)
    row = StoredRoute.objects.get()
    assert row.hit_count == 2
    assert len(fake_ors.calls) == 1


def test_stored_geometry_round_trips_exactly_and_is_compressed(client, fake_ors):
    get_route(client, **CHI_DEN)
    row = StoredRoute.objects.get()
    original = load_route_fixture("chicago_denver")["features"][0]["geometry"]["coordinates"]
    assert row.coordinates == original
    assert row.stored_kb < len(str(original)) / 1024 / 2
    assert row.profile == "driving-hgv" and row.status == LookupStatus.OK


def test_expired_route_is_refreshed_with_one_call(client, fake_ors):
    get_route(client, **CHI_DEN)
    get_route(client, **CHI_DEN)
    backdate(StoredRoute, 31)

    refreshed = get_route(client, **CHI_DEN).json()
    assert refreshed["meta"]["external_calls"] == 1 and refreshed["meta"]["route_source"] == "api"
    row = StoredRoute.objects.get()
    assert timezone.now() - row.fetched_at < timedelta(minutes=1)
    assert row.hit_count == 1  # earlier reuse count is kept


def test_route_within_max_age_is_not_refreshed(client, fake_ors):
    get_route(client, **CHI_DEN)
    backdate(StoredRoute, 29)
    assert get_route(client, **CHI_DEN).json()["meta"]["external_calls"] == 0


@pytest.mark.parametrize("outage", ["network", "rate_limit", "bad_key"])
def test_expired_route_is_served_when_provider_fails(client, fake_ors, settings, outage):
    get_route(client, **CHI_DEN)
    backdate(StoredRoute, 45)
    if outage == "network":
        fake_ors.error = requests.ConnectionError("down")
    elif outage == "rate_limit":
        fake_ors.responses.update({p: FakeResponse(429, {"error": "Rate limit exceeded"}) for p in fake_ors.PROFILES})
    else:
        settings.ORS_API_KEY = ""

    response = get_route(client, **CHI_DEN)
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["route_source"] == "stored_stale"
    assert body["fuel"]["stop_count"] >= 2


def test_no_route_answer_is_remembered_then_retried_after_a_day(client, fake_ors):
    params = {"start": "Honolulu, HI", "finish": "Chicago, IL"}
    assert get_route(client, **params).status_code == 422
    assert len(fake_ors.directions_calls) == 2  # truck, then car
    assert StoredRoute.objects.get().status == LookupStatus.NOT_FOUND

    second = get_route(client, **params)
    assert second.status_code == 422 and len(fake_ors.directions_calls) == 2

    backdate(StoredRoute, 2)
    assert get_route(client, **params).status_code == 422
    assert len(fake_ors.directions_calls) == 4


def test_geocoded_address_is_stored_and_reused(client, fake_ors):
    fake_ors.places[ADDRESS.lower()] = (39.76185, -104.881105, "1437 Bannock St, Denver, CO, USA")
    first = get_route(client, start="Chicago, IL", finish=ADDRESS).json()
    assert first["meta"]["external_calls"] == 2 and first["meta"]["geocode_sources"] == ["api"]

    simulate_restart()
    again = get_route(client, start="Chicago, IL", finish="  1437   BANNOCK st, denver ").json()
    assert again["meta"]["external_calls"] == 0
    assert again["meta"]["geocode_sources"] == ["stored"]
    assert StoredGeocode.objects.get().hit_count == 1


def test_unknown_address_is_remembered(client, fake_ors):
    assert get_route(client, start="Nowhereville Xyz", finish="Denver, CO").status_code == 400
    assert get_route(client, start="Nowhereville Xyz", finish="Denver, CO").status_code == 400
    assert len(fake_ors.calls) == 1
    assert StoredGeocode.objects.get().status == LookupStatus.NOT_FOUND


@pytest.mark.parametrize(
    "reply",
    [
        FakeResponse(429, {"error": "Rate limit exceeded"}),
        FakeResponse(500, {"error": "Internal error"}),
        FakeResponse(403, {"error": "Access to this API has been disallowed"}),
    ],
)
def test_transient_failures_leave_nothing_stored(client, fake_ors, reply):
    fake_ors.responses.update({p: reply for p in fake_ors.PROFILES})
    assert get_route(client, **CHI_DEN).status_code in (502, 503)
    assert StoredRoute.objects.count() == 0

    fake_ors.responses.clear()
    assert get_route(client, **CHI_DEN).json()["meta"]["external_calls"] == 1


def test_timeout_leaves_nothing_stored(client, fake_ors):
    fake_ors.error = requests.Timeout("slow")
    assert get_route(client, **CHI_DEN).status_code == 502
    assert StoredRoute.objects.count() == 0


# --- management command and admin -------------------------------------------


def run(*args):
    out = io.StringIO()
    call_command("routes", *args, stdout=out)
    return out.getvalue()


def test_prefetch_stores_new_routes_and_skips_stored_ones(fake_ors):
    out = run("prefetch", "Chicago, IL", "Denver, CO", "Dallas, TX", "Austin, TX")
    assert "Done; 2 API calls made." in out
    assert StoredRoute.objects.count() == 2

    out = run("prefetch", "Chicago, IL", "Denver, CO", "Dallas, TX", "Austin, TX")
    assert out.count(": stored (0 API calls)") == 2 and "Done; 0 API calls made." in out


def test_prefetch_reports_failures_and_rejects_odd_arguments(fake_ors):
    from django.core.management.base import CommandError

    assert "route_not_found" in run("prefetch", "Honolulu, HI", "Chicago, IL")
    with pytest.raises(CommandError):
        run("prefetch", "Chicago, IL")


def test_stats_and_clear(fake_ors):
    run("prefetch", "Chicago, IL", "Denver, CO", "Dallas, TX", "Austin, TX", "Honolulu, HI", "Chicago, IL")
    stats = run("stats")
    assert "Routes: 3 stored (2 ok, 1 not found)" in stats and "KB of geometry" in stats

    assert "Deleted 0" in run("clear", "--expired")
    StoredRoute.objects.filter(status=LookupStatus.NOT_FOUND).update(fetched_at=timezone.now() - timedelta(days=2))
    assert "Deleted 1" in run("clear", "--expired")
    assert StoredRoute.objects.count() == 2

    assert "Deleted 2" in run("clear", "--all")
    assert StoredRoute.objects.count() == 0


def test_admin_pages_load(admin_client, client, fake_ors):
    fake_ors.places[ADDRESS.lower()] = (39.76185, -104.881105, "1437 Bannock St, Denver, CO, USA")
    get_route(client, start="Chicago, IL", finish=ADDRESS)
    route = StoredRoute.objects.get()
    for url in (
        reverse("admin:routing_storedroute_changelist"),
        reverse("admin:routing_storedroute_change", args=[route.pk]),
        reverse("admin:routing_storedgeocode_changelist"),
    ):
        assert admin_client.get(url).status_code == 200, url
