from django.urls import path

from .views import dashboard, mesecni_obracun, mesecni_obracun_predogled_pdf

app_name = "ui"

urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("mesecni-obracun/", mesecni_obracun, name="mesecni_obracun"),
    path(
        "mesecni-obracun/predogled-pdf/",
        mesecni_obracun_predogled_pdf,
        name="mesecni_obracun_predogled_pdf",
    ),
]
