from django.urls import path

from .views import (
    dashboard,
    ddv_rezim_aktiviraj,
    ddv_rezim_deaktiviraj,
    ddv_rezim_detail,
    ddv_rezim_nov,
    ddv_rezimi,
    mesecni_obracun,
    mesecni_obracun_predogled_pdf,
    nastavitve,
    podjetje_arhiviraj,
    podjetje_detail,
    podjetje_novo,
    podjetje_reaktiviraj,
    podpisnik_detail,
    podpisnik_nov,
    storitev_aktiviraj,
    storitev_deaktiviraj,
    storitev_detail,
    storitev_nova,
    storitve,
)

app_name = "ui"

urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("nastavitve/", nastavitve, name="nastavitve"),
    path("nastavitve/ddv/", ddv_rezimi, name="ddv_rezimi"),
    path("nastavitve/ddv/nov/", ddv_rezim_nov, name="ddv_rezim_nov"),
    path("nastavitve/ddv/<int:regime_id>/", ddv_rezim_detail, name="ddv_rezim_detail"),
    path("nastavitve/ddv/<int:regime_id>/deaktiviraj/", ddv_rezim_deaktiviraj, name="ddv_rezim_deaktiviraj"),
    path("nastavitve/ddv/<int:regime_id>/aktiviraj/", ddv_rezim_aktiviraj, name="ddv_rezim_aktiviraj"),
    path("nastavitve/storitve/", storitve, name="storitve"),
    path("nastavitve/storitve/nova/", storitev_nova, name="storitev_nova"),
    path("nastavitve/storitve/<int:item_id>/", storitev_detail, name="storitev_detail"),
    path("nastavitve/storitve/<int:item_id>/deaktiviraj/", storitev_deaktiviraj, name="storitev_deaktiviraj"),
    path("nastavitve/storitve/<int:item_id>/aktiviraj/", storitev_aktiviraj, name="storitev_aktiviraj"),
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
