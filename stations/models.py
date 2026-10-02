from django.db import models


class FuelStation(models.Model):
    """A US truck stop with its fuel price and an approximate (city-level) location."""

    class GeocodeSource(models.TextChoices):
        MANUAL = "manual", "Manual override"
        GAZETTEER = "gazetteer", "Census Gazetteer"
        NOMINATIM = "nominatim", "Nominatim"

    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    retail_price = models.DecimalField(max_digits=7, decimal_places=4)
    latitude = models.FloatField()
    longitude = models.FloatField()
    geocode_source = models.CharField(max_length=16, choices=GeocodeSource.choices)

    class Meta:
        ordering = ["opis_id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.retail_price}"
