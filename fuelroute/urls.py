from urllib.parse import urlencode

from django.contrib import admin
from django.shortcuts import redirect
from django.urls import include, path, reverse
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView


def home(request):
    """Send visitors to an example map (RedirectView would choke on '%20' in a literal URL)."""
    return redirect(f"{reverse('route-map')}?{urlencode({'start': 'Chicago, IL', 'finish': 'Denver, CO'})}")


urlpatterns = [
    path("", home),
    path("admin/", admin.site.urls),
    path("api/v1/", include("routing.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]
