from django.urls import path

from .views import dashboard, mesecni_obracun

app_name = "ui"

urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("mesecni-obracun/", mesecni_obracun, name="mesecni_obracun"),
]
