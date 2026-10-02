from django.contrib import admin

from routing.models import StoredGeocode, StoredRoute


@admin.register(StoredRoute)
class StoredRouteAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "status",
        "profile",
        "distance_miles",
        "stored_kb_display",
        "hit_count",
        "fetched_at",
        "last_used_at",
    )
    list_filter = ("status", "profile")
    readonly_fields = [f.name for f in StoredRoute._meta.fields if f.name != "geometry_zlib"]
    exclude = ("geometry_zlib",)
    ordering = ("-last_used_at",)

    @admin.display(description="Stored KB")
    def stored_kb_display(self, obj):
        return round(obj.stored_kb, 1)


@admin.register(StoredGeocode)
class StoredGeocodeAdmin(admin.ModelAdmin):
    list_display = ("query", "status", "label", "lat", "lon", "hit_count", "fetched_at", "last_used_at")
    list_filter = ("status",)
    search_fields = ("query", "label")
    readonly_fields = [f.name for f in StoredGeocode._meta.fields]
    ordering = ("-last_used_at",)
