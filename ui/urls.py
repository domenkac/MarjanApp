from django.urls import path

from .views import (
    dashboard,
    mesecni_obracun,
    mesecni_obracun_predogled_pdf,
    nastavitve,
    podjetje_arhiviraj,
    podjetje_detail,
    podjetje_novo,
    podjetje_reaktiviraj,
    podpisnik_detail,
    podpisnik_nov,
)

app_name = "ui"

urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("nastavitve/", nastavitve, name="nastavitve"),
    path("nastavitve/podjetja/novo/", podjetje_novo, name="podjetje_novo"),
    path("nastavitve/podjetja/<int:company_id>/", podjetje_detail, name="podjetje_detail"),
    path("nastavitve/podjetja/<int:company_id>/arhiviraj/", podjetje_arhiviraj, name="podjetje_arhiviraj"),
    path("nastavitve/podjetja/<int:company_id>/reaktiviraj/", podjetje_reaktiviraj, name="podjetje_reaktiviraj"),
    path("nastavitve/podjetja/<int:company_id>/podpisniki/nov/", podpisnik_nov, name="podpisnik_nov"),
    path("nastavitve/podpisniki/<int:signatory_id>/", podpisnik_detail, name="podpisnik_detail"),
    path("mesecni-obracun/", mesecni_obracun, name="mesecni_obracun"),
    path(
        "mesecni-obracun/predogled-pdf/",
        mesecni_obracun_predogled_pdf,
        name="mesecni_obracun_predogled_pdf",
    ),
]
