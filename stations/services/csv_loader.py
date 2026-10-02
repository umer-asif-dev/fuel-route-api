"""Parse and clean the OPIS fuel price CSV."""

import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .geocode import US_STATES


@dataclass(frozen=True)
class StationRecord:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    rack_id: int | None
    retail_price: Decimal


@dataclass
class LoadReport:
    rows_read: int = 0
    non_us_rows: int = 0
    invalid_rows: int = 0
    duplicate_rows: int = 0


def load_station_records(path: Path) -> tuple[list[StationRecord], LoadReport]:
    """Read the CSV and return one record per US station.

    The file lists some stations several times with different prices. We keep
    the cheapest row for each OPIS ID, since that is the price a fleet would pay.
    Rows outside the 50 states + DC (Canadian provinces) are dropped.
    """
    report = LoadReport()
    best: dict[int, StationRecord] = {}

    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            report.rows_read += 1
            state = row["State"].strip().upper()
            if state not in US_STATES:
                report.non_us_rows += 1
                continue
            try:
                record = StationRecord(
                    opis_id=int(row["OPIS Truckstop ID"]),
                    name=row["Truckstop Name"].strip(),
                    address=row["Address"].strip(),
                    city=" ".join(row["City"].split()),
                    state=state,
                    rack_id=int(row["Rack ID"]) if row["Rack ID"].strip() else None,
                    retail_price=Decimal(row["Retail Price"].strip()),
                )
            except (ValueError, InvalidOperation, KeyError):
                report.invalid_rows += 1
                continue
            if record.retail_price <= 0:
                report.invalid_rows += 1
                continue

            existing = best.get(record.opis_id)
            if existing is not None:
                report.duplicate_rows += 1
                if record.retail_price >= existing.retail_price:
                    continue
            best[record.opis_id] = record

    return sorted(best.values(), key=lambda r: r.opis_id), report
