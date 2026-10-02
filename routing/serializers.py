from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(max_length=200, help_text='"City, ST", "lat,lon" or a street address in the USA.')
    finish = serializers.CharField(max_length=200, help_text='"City, ST", "lat,lon" or a street address in the USA.')
    corridor_miles = serializers.FloatField(
        required=False,
        min_value=1,
        max_value=50,
        help_text="How far off the route a station may be (default 10).",
    )


class ErrorDetailSerializer(serializers.Serializer):
    code = serializers.CharField(help_text="Machine-readable error code, e.g. location_outside_usa.")
    message = serializers.CharField()
    details = serializers.DictField(required=False)


class ErrorSerializer(serializers.Serializer):
    error = ErrorDetailSerializer()
