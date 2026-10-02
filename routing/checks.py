from django.conf import settings
from django.core.checks import Warning, register


@register()
def ors_api_key_check(app_configs, **kwargs):
    if settings.ORS_API_KEY:
        return []
    return [
        Warning(
            "ORS_API_KEY is not set; route requests that aren't cached will return 503.",
            hint="Get a free key at https://openrouteservice.org/dev/#/signup and add it to .env.",
            id="routing.W001",
        )
    ]
