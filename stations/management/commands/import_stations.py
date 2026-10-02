import csv
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from stations.models import FuelStation
from stations.services.csv_loader import load_station_records
from stations.services.geocode import Gazetteer
from stations.services.nominatim import CachedNominatim


class Command(BaseCommand):
    help = (
        "Clean the fuel price CSV, geocode each station to its city, and load it into the "
        "database. Checks manual overrides first, then the offline Census Gazetteer, then a "
        "cached Nominatim lookup for the few cities it lacks. Safe to re-run."
    )

    def add_arguments(self, parser):
        parser.add_argument("csv_path", nargs="?", type=Path, default=settings.FUEL_PRICES_CSV)
        parser.add_argument(
            "--offline",
            action="store_true",
            help="Never call Nominatim; stations whose city isn't cached are skipped.",
        )

    def handle(self, *args, **options):
        csv_path: Path = options["csv_path"]
        if not csv_path.exists():
            raise CommandError(f"CSV not found: {csv_path}")
        if not settings.GAZETTEER_CSV.exists():
            raise CommandError(f"{settings.GAZETTEER_CSV} missing; run `manage.py build_gazetteer` first.")

        records, report = load_station_records(csv_path)
        self.stdout.write(
            f"Read {report.rows_read} rows: {len(records)} unique US stations, "
            f"{report.duplicate_rows} duplicate rows, {report.non_us_rows} non-US rows, "
            f"{report.invalid_rows} invalid rows."
        )

        overrides = {
            key: (value["lat"], value["lon"])
            for key, value in json.loads(settings.GEOCODE_OVERRIDES_JSON.read_text()).items()
        }
        gazetteer = Gazetteer.from_csv(settings.GAZETTEER_CSV)
        nominatim = CachedNominatim(settings.GEOCODE_CACHE_JSON)
        stations, unmatched = [], set()
        sources = dict.fromkeys(FuelStation.GeocodeSource.values, 0)

        try:
            for record in records:
                coords = overrides.get(f"{record.city.upper()}|{record.state}")
                source = FuelStation.GeocodeSource.MANUAL
                if coords is None:
                    coords = gazetteer.lookup(record.city, record.state)
                    source = FuelStation.GeocodeSource.GAZETTEER
                if coords is None:
                    coords = nominatim.lookup(record.city, record.state, allow_network=not options["offline"])
                    source = FuelStation.GeocodeSource.NOMINATIM
                if coords is None:
                    unmatched.add(f"{record.city}, {record.state}")
                    continue
                sources[source] += 1
                stations.append(
                    FuelStation(
                        opis_id=record.opis_id,
                        name=record.name,
                        address=record.address,
                        city=record.city,
                        state=record.state,
                        rack_id=record.rack_id,
                        retail_price=record.retail_price,
                        latitude=coords[0],
                        longitude=coords[1],
                        geocode_source=source,
                    )
                )
        finally:
            nominatim.save()

        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(stations, batch_size=1000)

        self._export(stations)
        self.stdout.write(
            f"Geocoded {len(stations)} stations ({sources['manual']} manual, {sources['gazetteer']} "
            f"gazetteer, {sources['nominatim']} nominatim; {nominatim.calls_made} network calls)."
        )
        if unmatched:
            self.stdout.write(self.style.WARNING(f"Skipped {len(unmatched)} unmatched cities: {sorted(unmatched)}"))
        self.stdout.write(self.style.SUCCESS(f"Loaded {len(stations)} stations."))

    def _export(self, stations: list[FuelStation]) -> None:
        with open(settings.STATIONS_GEOCODED_CSV, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                ["opis_id", "name", "city", "state", "retail_price", "latitude", "longitude", "geocode_source"]
            )
            for s in stations:
                writer.writerow(
                    [
                        s.opis_id,
                        s.name,
                        s.city,
                        s.state,
                        s.retail_price,
                        f"{s.latitude:.6f}",
                        f"{s.longitude:.6f}",
                        s.geocode_source,
                    ]
                )
