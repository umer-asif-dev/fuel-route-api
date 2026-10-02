from decimal import Decimal

from stations.services.csv_loader import load_station_records
from stations.services.geocode import Gazetteer, normalize_city, strip_place_suffix

CSV_HEADER = "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"


def write_csv(tmp_path, rows):
    path = tmp_path / "prices.csv"
    path.write_text(CSV_HEADER + "\n".join(rows) + "\n")
    return path


def test_duplicates_keep_cheapest_price_and_canada_is_dropped(tmp_path):
    path = write_csv(
        tmp_path,
        [
            '105,TA SAGINAW,"I-75, EXIT 144-B",Bridgeport,MI,260,3.339',
            '105,TA SAGINAW,"I-75, EXIT 144-B",Bridgeport,MI,260,3.269',
            '105,TA SAGINAW,"I-75, EXIT 144-B",Bridgeport,MI,260,3.429',
            "629,FLYING J #850,TCH-16,Edmonton,AB,80,4.39948962",
            "79,DELAWARE TRUCK PLAZA,US-13 & US-40,New Castle                              ,DE,243,3.249",
        ],
    )
    records, report = load_station_records(path)

    assert [r.opis_id for r in records] == [79, 105]
    assert records[1].retail_price == Decimal("3.269")
    assert records[0].city == "New Castle"
    assert report.duplicate_rows == 2
    assert report.non_us_rows == 1


def test_normalize_city_handles_spacing_and_abbreviations():
    assert normalize_city("Mc Calla") == normalize_city("McCalla")
    assert normalize_city("Saint Joseph") == normalize_city("St. Joseph")
    assert normalize_city("Fort Smith") == normalize_city("Ft Smith")
    assert normalize_city("  New Castle  ") == "newcastle"


def test_strip_place_suffix():
    assert strip_place_suffix("Abbeville city") == "Abbeville"
    assert strip_place_suffix("Abanda CDP") == "Abanda"
    assert strip_place_suffix("Nashville-Davidson metropolitan government (balance)") == "Nashville-Davidson"


def test_gazetteer_prefers_lower_rank_and_is_state_scoped():
    gaz = Gazetteer()
    gaz.add("TX", "Springfield", 1.0, 1.0, rank=5)
    gaz.add("TX", "Springfield", 2.0, 2.0, rank=1)
    gaz.add("IL", "Springfield", 3.0, 3.0, rank=1)
    assert gaz.lookup("springfield", "tx") == (2.0, 2.0)
    assert gaz.lookup("Springfield", "IL") == (3.0, 3.0)
    assert gaz.lookup("Springfield", "OH") is None


def test_committed_gazetteer_resolves_major_cities():
    from django.conf import settings

    gaz = Gazetteer.from_csv(settings.GAZETTEER_CSV)
    lat, lon = gaz.lookup("Chicago", "IL")
    assert 41.5 < lat < 42.2 and -88.0 < lon < -87.4
    assert gaz.lookup("Nashville", "TN") is not None
    assert gaz.lookup("Saint Louis", "MO") is not None
    assert gaz.lookup("Honolulu", "HI") is not None
