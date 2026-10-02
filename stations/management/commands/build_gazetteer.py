from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from stations.services.geocode import Gazetteer


class Command(BaseCommand):
    help = (
        "Build data/us_places.csv from the raw Census Gazetteer files "
        "(2025_Gaz_place_national.txt and 2025_Gaz_cousubs_national.txt)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--places", type=Path, default=settings.DATA_DIR / "2025_Gaz_place_national.txt")
        parser.add_argument("--cousubs", type=Path, default=settings.DATA_DIR / "2025_Gaz_cousubs_national.txt")
        parser.add_argument("--out", type=Path, default=settings.GAZETTEER_CSV)

    def handle(self, *args, **options):
        cousubs = options["cousubs"] if options["cousubs"].exists() else None
        gazetteer = Gazetteer.from_census_files(options["places"], cousubs)
        gazetteer.to_csv(options["out"])
        self.stdout.write(self.style.SUCCESS(f"Wrote {len(gazetteer)} places to {options['out']}"))
