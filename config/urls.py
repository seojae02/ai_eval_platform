from django.urls import include, path

urlpatterns = [
    path("", include("accounts.urls")),
    path("competitions/", include("competitions.urls")),
    path("competitions/", include("submissions.urls")),
]
