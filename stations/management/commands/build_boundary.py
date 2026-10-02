import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from routing.services.us_boundary import rings_from_shapefile


class Command(BaseCommand):
    help = "Convert the Census 1:20M national boundary shapefile into data/us_boundary.json."

    def add_arguments(self, parser):
        parser.add_argument("--shp", type=Path, default=settings.DATA_DIR / "nation" / "cb_2024_us_nation_20m.shp")

    def handle(self, *args, **options):
        rings = rings_from_shapefile(options["shp"])
        payload = {"source": "US Census Bureau cb_2024_us_nation_20m", "rings": rings}
        settings.US_BOUNDARY_JSON.write_text(json.dumps(payload, separators=(",", ":")))
        points = sum(len(r) for r in rings)
        self.stdout.write(
            self.style.SUCCESS(f"Wrote {len(rings)} rings / {points} points to {settings.US_BOUNDARY_JSON}")
        )
