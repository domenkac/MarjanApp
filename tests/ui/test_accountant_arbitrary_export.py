from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest
from django.db import connection
from django.urls import reverse

from accounting_export.arbitrary_export import prepare_arbitrary_accountant_exports
from accounting_export.csv_renderer import render_arbitrary_accountant_csv
from companies.models import Company
from invoicing.models import Invoice
from tests.accounting_export.test_arbitrary_export import (
    GENERATED_AT,
    invoice,
    read_only_export_execute,
)
from tests.accounting_export.test_report_snapshot import (
    company as company,  # noqa: PLC0414
)
from tests.accounting_export.test_task_state import forbid_side_effects

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr("django.utils.timezone.now", lambda: GENERATED_AT)


def query(company, **changes):
    return {"od": "2026-09-15", "do": "2026-10-12", "company_id": company.pk} | changes


def selection(companies, **changes):
    return {"od": "2026-09-15", "do": "2026-10-12", "podjetja": [item.pk for item in companies]} | changes


def test_page_fields_multiple_inactive_and_secondary_navigation(client, company):
    inactive = Company.objects.create(naziv="Inactive", davcna_stevilka="87654321", aktivno=False)
    response = client.get(reverse("ui:accountant_arbitrary_export"))
    assert response.status_code == 200
    html = response.content.decode()
    assert 'type="date" name="od"' in html
    assert 'type="date" name="do"' in html
    assert 'name="podjetja"' in html and 'multiple' in html
    assert "Inactive" in html
    form = response.context["form"]
    assert list(form.fields["podjetja"].queryset.values_list("pk", flat=True)) == [company.pk, inactive.pk]
    sidebar = html.split('<nav class="nav-list">', 1)[1].split("</nav>", 1)[0]
    assert sidebar.count('class="nav-item') == 4
    assert "Izpis za poljubno obdobje" not in sidebar
    dashboard = client.get(reverse("ui:dashboard")).content.decode()
    assert f'href="{reverse("ui:accountant_arbitrary_export")}"' in dashboard


@pytest.mark.parametrize("changes", [
    {"od": "2026-10-13"}, {"od": "bad"}, {"do": "2026-02-30"},
    {"do": "9999-12-31"}, {"podjetja": []}, {"podjetja": ["999999"]},
])
def test_invalid_selection_shows_safe_validation(client, company, changes):
    response = client.get(reverse("ui:accountant_arbitrary_export"), selection([company], **changes))
    assert response.status_code == 200
    assert response.context["form"].errors
    assert not response.context["results"]
    assert "Traceback" not in response.content.decode()


def test_multicompany_results_failure_and_empty_keep_valid_links(client, company):
    invoice(company, issued=date(2026, 9, 15), izdajatelj_naziv="Zgodovinska Alfa")
    conflict = Company.objects.create(naziv="Conflict", davcna_stevilka="87654321")
    empty = Company.objects.create(naziv="Empty", davcna_stevilka="11111111")
    invoice(conflict, issued=date(2026, 9, 15))
    invoice(conflict, number="NP-2", issued=date(2026, 9, 16), izdajatelj_naziv="Different")
    response = client.get(reverse("ui:accountant_arbitrary_export"), selection([company, conflict, empty]))
    assert response.status_code == 200
    results = response.context["results"]
    assert len(results) == 3
    by_id = {item["result"].company_id: item for item in results}
    assert by_id[company.pk]["result"].code == "READY"
    assert by_id[conflict.pk]["result"].code == "ISSUER_CONFLICT"
    assert by_id[empty.pk]["result"].code == "NO_ISSUED_DOCUMENTS"
    html = response.content.decode()
    assert "Zgodovinska Alfa" in html
    assert "Prenesi CSV" in html
    assert "V izbranem obdobju ni izdanih dokumentov." in html
    assert not by_id[conflict.pk]["download_url"]
    link = by_id[company.pk]["download_url"]
    assert parse_qs(urlsplit(link).query) == {
        "od": ["2026-09-15"], "do": ["2026-10-12"], "company_id": [str(company.pk)],
    }
    assert client.get(link).status_code == 200


def test_download_exact_bytes_headers_and_no_writes(client, company, monkeypatch):
    invoice(company, issued=date(2026, 9, 15), izdajatelj_naziv="Živa Alfa d.o.o.")
    export = prepare_arbitrary_accountant_exports(
        date_from=date(2026, 9, 15), date_to=date(2026, 10, 12), company_ids=[company.pk],
        generated_at=GENERATED_AT,
    ).companies[0].export
    expected = render_arbitrary_accountant_csv(export)
    forbid_side_effects(monkeypatch)
    with connection.execute_wrapper(read_only_export_execute):
        response = client.get(reverse("ui:accountant_arbitrary_csv"), query(company))
    assert response.status_code == 200
    assert response.content == expected
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"] == 'attachment; filename="2026-09-15_2026-10-12-Ziva-Alfa-doo.csv"'
    assert response.content.startswith(b"\xef\xbb\xbf")


def test_download_reselects_canonical_evidence(client, company):
    item = invoice(company, issued=date(2026, 9, 15))
    client.get(reverse("ui:accountant_arbitrary_export"), selection([company]))
    Invoice.objects.filter(pk=item.pk).update(status=Invoice.Status.OSNUTEK)
    response = client.get(reverse("ui:accountant_arbitrary_csv"), query(company))
    assert response.status_code == 400
    assert "V izbranem obdobju ni izdanih dokumentov." in response.content.decode()
    assert "Content-Disposition" not in response


@pytest.mark.parametrize("changes", [
    {"od": "bad"}, {"do": "2026-02-30"}, {"od": "2026-10-13"},
    {"company_id": "bad"}, {"company_id": "999999"}, {"company_id": ""},
    {"company_id": "-1"}, {"company_id": str(2**100)}, {"do": "9999-12-31"},
    {"od": ""},
])
def test_download_malformed_query_safe(client, company, changes):
    with connection.execute_wrapper(read_only_export_execute):
        response = client.get(reverse("ui:accountant_arbitrary_csv"), query(company, **changes))
    assert response.status_code == 400
    assert "Content-Disposition" not in response
    assert "Traceback" not in response.content.decode()


def test_download_conflict_safe_and_filename_header_injection_protected(client, company):
    item = invoice(company, issued=date(2026, 9, 15), izdajatelj_naziv='Živa/../Alfa\\x;"\r\nInjected: yes')
    response = client.get(reverse("ui:accountant_arbitrary_csv"), query(company))
    assert response.status_code == 200
    disposition = response["Content-Disposition"]
    assert "\r" not in disposition and "\n" not in disposition and "\\" not in disposition and "/" not in disposition
    invoice(company, number="NP-2", issued=date(2026, 9, 16), izdajatelj_naziv="Different")
    response = client.get(reverse("ui:accountant_arbitrary_csv"), query(company))
    assert response.status_code == 400
    assert item.izdajatelj_naziv not in response.content.decode()


def test_page_preparation_only_selects(client, company, monkeypatch):
    invoice(company, issued=date(2026, 9, 15))
    # Warm template loading before forbidding external operations.
    client.get(reverse("ui:accountant_arbitrary_export"))
    forbid_side_effects(monkeypatch)
    with connection.execute_wrapper(read_only_export_execute):
        response = client.get(reverse("ui:accountant_arbitrary_export"), selection([company]))
    assert response.status_code == 200
    assert response.context["results"][0]["result"].code == "READY"


def test_endpoints_get_only(client, company):
    for name in ["ui:accountant_arbitrary_export", "ui:accountant_arbitrary_csv"]:
        assert client.post(reverse(name), query(company)).status_code == 405
