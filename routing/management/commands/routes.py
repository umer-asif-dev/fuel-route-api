from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max, Min, Sum
from django.utils import timezone

from routing.exceptions import RoutePlanningError
from routing.models import LookupStatus, StoredGeocode, StoredRoute
from routing.services.planner import RoutePlanner


class Command(BaseCommand):
    help = "Manage locally stored OpenRouteService results: prefetch, stats, clear."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)

        prefetch = sub.add_parser("prefetch", help="Fetch and store routes for START FINISH pairs.")
        prefetch.add_argument("places", nargs="+", help='Pairs: "Chicago, IL" "Denver, CO" [START FINISH ...]')

        sub.add_parser("stats", help="Show what is stored.")

        clear = sub.add_parser("clear", help="Delete stored results so they are fetched again.")
        group = clear.add_mutually_exclusive_group(required=True)
        group.add_argument("--expired", action="store_true", help="Only entries past their max age.")
        group.add_argument("--not-found", action="store_true", help="Only remembered 'not found' answers.")
        group.add_argument("--all", action="store_true", help="Everything.")

    def handle(self, *args, action, **options):
        getattr(self, f"_{action}")(**options)

    def _prefetch(self, places, **_):
        if len(places) % 2:
            raise CommandError("Give places in START FINISH pairs.")
        planner = RoutePlanner()
        total_calls = 0
        for start, finish in zip(places[::2], places[1::2], strict=True):
            try:
                meta = planner.plan(start, finish)["meta"]
            except RoutePlanningError as exc:
                self.stdout.write(self.style.WARNING(f"{start} -> {finish}: {exc.code}: {exc.message}"))
                continue
            total_calls += meta["external_calls"]
            self.stdout.write(f"{start} -> {finish}: {meta['route_source']} ({meta['external_calls']} API calls)")
        self.stdout.write(self.style.SUCCESS(f"Done; {total_calls} API calls made."))

    def _stats(self, **_):
        for model, label in ((StoredRoute, "Routes"), (StoredGeocode, "Geocodes")):
            qs = model.objects.all()
            agg = qs.aggregate(oldest=Min("fetched_at"), newest=Max("fetched_at"), hits=Sum("hit_count"))
            ok = qs.filter(status=LookupStatus.OK).count()
            line = f"{label}: {qs.count()} stored ({ok} ok, {qs.count() - ok} not found), {agg['hits'] or 0} reuses"
            if model is StoredRoute and qs.exists():
                size_kb = sum(r.stored_kb for r in qs.only("geometry_zlib"))
                line += f", {size_kb:.0f} KB of geometry"
            if agg["oldest"]:
                line += f", fetched {agg['oldest']:%Y-%m-%d %H:%M} to {agg['newest']:%Y-%m-%d %H:%M} UTC"
            self.stdout.write(line)

    def _clear(self, expired=False, not_found=False, all=False, **_):
        deleted = 0
        for model in (StoredRoute, StoredGeocode):
            qs = model.objects.all()
            if expired:
                now = timezone.now()
                qs = qs.filter(
                    status=LookupStatus.OK, fetched_at__lt=now - timedelta(days=settings.ROUTE_STORE_MAX_AGE_DAYS)
                ) | qs.filter(
                    status=LookupStatus.NOT_FOUND,
                    fetched_at__lt=now - timedelta(days=settings.NEGATIVE_STORE_MAX_AGE_DAYS),
                )
            elif not_found:
                qs = qs.filter(status=LookupStatus.NOT_FOUND)
            deleted += qs.delete()[0]
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} stored entries."))
