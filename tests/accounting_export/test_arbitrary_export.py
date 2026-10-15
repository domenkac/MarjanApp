import codecs
import csv
import hashlib
import re
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.utils import timezone

from accounting_export.arbitrary_export import prepare_arbitrary_accountant_exports
from accounting_export.csv_renderer import render_arbitrary_accountant_csv
from accounting_export.models import AccountantReport, AccountantReportPdfArtifact
from accounting_export.task_state import accountant_control_dashboard_tasks
from companies.models import Company
from delivery.models import AccountantReportDeliveryAttempt
from invoicing.models import Invoice, InvoiceLine, MonthlyBatch, MonthlyCycle
from tests.accounting_export.test_csv_renderer import HEADER
from tests.accounting_export.test_csv_renderer import report as report  # noqa: PLC0414
from tests.accounting_export.test_report_snapshot import _invoice, _line
from tests.accounting_export.test_report_snapshot import (
    company as company,  # noqa: PLC0414
)
from tests.accounting_export.test_task_state import forbid_side_effects
from tests.financial_fixtures import update_financial_fixture

pytestmark = pytest.mark.django_db
GENERATED_AT = datetime(2026, 10, 13, 0, 30, tzinfo=UTC)


def invoice(company, **changes):
    item = _invoice(company, completed_evidence=False, **changes)
    _line(item)
    return item


def prepare(companies, start=date(2026, 9, 15), end=date(2026, 10, 12), **changes):
    return prepare_arbitrary_accountant_exports(**({
        "date_from": start, "date_to": end,
        "company_ids": [item.pk for item in companies], "generated_at": GENERATED_AT,
    } | changes))


def rows(export):
    return list(csv.DictReader(
        StringIO(render_arbitrary_accountant_csv(export).decode("utf-8-sig"), newline=""),
        delimiter=";",
    ))


def only_export(company, **changes):
    result = prepare([company], **changes).companies[0]
    assert result.code == "READY"
    return result.export


@pytest.mark.parametrize("start,end", [
    (date(2026, 9, 15), date(2026, 10, 12)),
    (date(2025, 12, 31), date(2026, 1, 1)),
    (date(2026, 9, 15), date(2026, 9, 15)),
])
def test_inclusive_range_and_only_selected_companies(company, start, end):
    from datetime import timedelta

    for index, issued in enumerate([start - timedelta(days=1), start, end, end + timedelta(days=1)]):
        invoice(company, number=f"NP-{index}", issued=issued)
    other = Company.objects.create(naziv="Other", davcna_stevilka="87654321")
    invoice(other, number="OTHER", issued=start)
    export = only_export(company, start=start, end=end)
    issued = [row for row in rows(export) if row["vrstica_tip"] == "RACUN"]
    assert [row["stevilka_racuna"] for row in issued] == ["NP-1", "NP-2"]
    assert export.issued_count == 2
    assert {row["obdobje_porocila"] for row in rows(export)} == {f"{start.isoformat()}_{end.isoformat()}"}


@pytest.mark.parametrize("start,end", [
    (date(2026, 10, 12), date(2026, 9, 15)),
    ("2026-09-15", date(2026, 10, 12)),
    (date(2026, 9, 15), None),
    (datetime(2026, 9, 15, tzinfo=UTC), date(2026, 10, 12)),
    (date(2026, 9, 15), date.max),
])
def test_invalid_range_rejected_before_queries(company, start, end, django_assert_num_queries):
    with django_assert_num_queries(0), pytest.raises(ValidationError):
        prepare([company], start, end)


@pytest.mark.parametrize("company_ids", [[], [True], ["1"], [None], [0], [-1], "1", None, [2**100]])
def test_invalid_company_ids_rejected(company, company_ids):
    with pytest.raises(ValidationError):
        prepare([company], company_ids=company_ids)


def test_unknown_company_rejected(company):
    with pytest.raises(ValidationError):
        prepare([company], company_ids=[company.pk + 1000])


@pytest.mark.parametrize("generated_at", [
    "2026-10-13", date(2026, 10, 13),
    datetime(2026, 10, 13, tzinfo=UTC).replace(tzinfo=None),
])
def test_invalid_generation_timestamp_rejected(company, generated_at):
    with pytest.raises(ValidationError):
        prepare([company], generated_at=generated_at)


@pytest.mark.parametrize("kind", Invoice.DocumentType.values)
@pytest.mark.parametrize("status", Invoice.Status.values)
def test_canonical_status_and_document_type_selection(company, kind, status):
    invoice(company, kind=kind, status=status, issued=date(2026, 9, 15))
    result = prepare([company]).companies[0]
    excluded = status in {"OSNUTEK", "ZA_PONOVNI_PREGLED", "ARHIVIRAN_NI_IZDAN"}
    assert result.code == ("NO_ISSUED_DOCUMENTS" if excluded else "READY")
    if not excluded:
        assert rows(result.export)[0]["tip"] == kind


@pytest.mark.parametrize("number", [None, ""])
def test_unnumbered_excluded(company, number):
    invoice(company, number=number, issued=date(2026, 9, 15))
    assert prepare([company]).companies[0].code == "NO_ISSUED_DOCUMENTS"


def test_inactive_and_historical_identity_isolated_with_shared_local_timestamp(company):
    other = Company.objects.create(naziv="Other", davcna_stevilka="87654321", aktivno=False)
    invoice(company, issued=date(2026, 9, 15), izdajatelj_naziv="Živa Alfa d.o.o.")
    invoice(other, number="ST-2", issued=date(2026, 10, 12), izdajatelj_naziv="Stara Beta",
            izdajatelj_davcna_stevilka="87654321", izdajatelj_maticna_stevilka="9999999000")
    update_financial_fixture(Company.objects.filter(pk=company.pk), naziv="Changed", davcna_stevilka="00000000")
    with timezone.override("Europe/Ljubljana"):
        result = prepare([company, other, company], generated_at=datetime(2026, 10, 12, 22, 30, tzinfo=UTC))
    assert len(result.companies) == 2
    first, second = (item.export for item in result.companies)
    assert first.generated_at == second.generated_at == result.generated_at
    assert first.generated_at.utcoffset().total_seconds() == 7200
    assert first.company_label == "Živa Alfa d.o.o."
    assert first.filename == "2026-09-15_2026-10-12-Ziva-Alfa-doo.csv"
    assert second.filename == "2026-09-15_2026-10-12-Stara-Beta.csv"
    for export, name, tax, registration, number in [
        (first, "Živa Alfa d.o.o.", "15012557", "1234567000", "NP-00001"),
        (second, "Stara Beta", "87654321", "9999999000", "ST-2"),
    ]:
        assert {row["podjetje"] for row in rows(export)} == {name}
        assert {row["podjetje_davcna_stevilka"] for row in rows(export)} == {tax}
        assert {row["podjetje_maticna_stevilka"] for row in rows(export)} == {registration}
        assert {row["datum_izdelave"] for row in rows(export)} == {"13.10.2026"}
        assert [row["stevilka_racuna"] for row in rows(export) if row["vrstica_tip"] == "RACUN"] == [number]


@pytest.mark.parametrize("field", ["izdajatelj_naziv", "izdajatelj_davcna_stevilka", "izdajatelj_maticna_stevilka"])
def test_identity_conflict_and_empty_do_not_block_siblings(company, field):
    invoice(company, issued=date(2026, 9, 15))
    invoice(company, number="NP-2", issued=date(2026, 9, 16), **{field: "DIFFERENT"})
    valid = Company.objects.create(naziv="Valid", davcna_stevilka="87654321")
    empty = Company.objects.create(naziv="Empty", davcna_stevilka="11111111")
    invoice(valid, issued=date(2026, 9, 15))
    result = prepare([company, valid, empty])
    assert [item.code for item in result.companies] == ["ISSUER_CONFLICT", "READY", "NO_ISSUED_DOCUMENTS"]
    assert result.companies[0].export is None
    assert result.companies[2].export is None
    assert result.companies[2].message == "V izbranem obdobju ni izdanih dokumentov."


def test_other_domain_rejection_does_not_block_siblings(company):
    # An issued document without line snapshots cannot produce the approved VAT row.
    _invoice(company, completed_evidence=False, issued=date(2026, 9, 15))
    valid = Company.objects.create(naziv="Valid", davcna_stevilka="87654321")
    invoice(valid, issued=date(2026, 9, 15))
    result = prepare([company, valid])
    assert [item.code for item in result.companies] == ["INVALID_DOCUMENTS", "READY"]
    assert result.companies[0].export is None


@pytest.mark.parametrize("start,end", [(date(2030, 1, 1), date(2031, 2, 2)), (date.min, date.min)])
def test_empty_current_or_future_range_has_no_live_fallback(company, start, end):
    result = prepare([company], start=start, end=end).companies[0]
    assert result.code == "NO_ISSUED_DOCUMENTS"
    assert result.export is None


def test_shared_csv_format_mixed_vat_recap_negative_and_escaping(company):
    item = invoice(company, issued=date(2026, 9, 15), prejemnik_naziv='Špela; "Černe"\r\nDruga vrstica')
    _line(item, method="OPROSCENO", rate="0.00", group="OPR_44", net="20.00", vat="0.00")
    _line(item, method="NI_PREDMET", rate="0.00", group="NI_PREDMET", net="10.00", vat="0.00")
    _line(item, group="LOW", rate="9.50", net="100.00", vat="9.50")
    for kind in ["STORNO", "DOBROPIS"]:
        negative = _invoice(company, completed_evidence=False, number=kind, kind=kind, issued=date(2026, 10, 12))
        _line(negative, net="-10.00", vat="-2.20")
    export = only_export(company)
    output = render_arbitrary_accountant_csv(export)
    assert output.startswith(codecs.BOM_UTF8)
    text = output.decode("utf-8-sig")
    assert text.split("\r\n")[0] == HEADER
    assert text.endswith("\r\n")
    assert "\n" not in text.replace("\r\n", "")
    parsed = rows(export)
    assert all(len(row) == 20 for row in parsed)
    issued = [row for row in parsed if row["vrstica_tip"] == "RACUN"]
    assert len(issued) == 3
    assert issued[0]["prejemnik"] == 'Špela; "Černe"\r\nDruga vrstica'
    assert issued[0]["datum_izdaje"] == "15.09.2026"
    assert issued[0]["obdobje_od"] == "01.09.2026"
    assert issued[0]["obdobje_do"] == "30.09.2026"
    assert issued[0]["ddv_rezim"] == (
        "NI_PREDMET 0,00 % [NI_PREDMET] | OBDAVCENO 9,50 % [LOW] | "
        "OBDAVCENO 22,00 % [STANDARD] | OPROSCENO 0,00 % [OPR_44]"
    )
    assert [issued[0][field] for field in ["osnova", "ddv", "skupaj"]] == ["230,00", "31,50", "261,50"]
    assert all(row["osnova"] == "-10,00" and row["skupaj"] == "-12,20" for row in issued[1:])
    recap = {row["ddv_rezim"]: row for row in parsed if row["vrstica_tip"] == "REKAPITULACIJA"}
    assert set(recap) == {"LOW / 9,50 %", "OPR_44 / 0,00 %", "STANDARD / 22,00 %"}
    assert recap["STANDARD / 22,00 %"]["osnova"] == "80,00"
    assert recap["STANDARD / 22,00 %"]["ddv"] == "17,60"
    assert all(row["vrstica_tip"] != "NEIZDANO" for row in parsed)


def test_filename_rejects_path_and_header_injection(company):
    invoice(company, issued=date(2026, 9, 15), izdajatelj_naziv='Živa/../Alfa\\x;"\r\n.csv')
    export = only_export(company)
    assert re.fullmatch(r"2026-09-15_2026-10-12-[A-Za-z0-9_-]+\.csv", export.filename)
    assert "/" not in export.filename and "\\" not in export.filename
    assert rows(export)[0]["podjetje"] == 'Živa/../Alfa\\x;"\r\n.csv'


def read_only_export_execute(execute, sql, params, many, context):
    assert sql.lstrip().split()[0].upper() == "SELECT", sql
    assert "contracts_lease" not in sql.lower()
    assert "invoicing_monthlycandidatereview" not in sql.lower()
    assert "catalogs_vatregime" not in sql.lower()
    return execute(sql, params, many, context)


def test_read_only_no_external_operations_and_dashboard_unchanged(company, monkeypatch):
    item = invoice(company, issued=date(2026, 9, 15))
    models = (Invoice, InvoiceLine, MonthlyCycle, MonthlyBatch, AccountantReport,
              AccountantReportPdfArtifact, AccountantReportDeliveryAttempt)
    before = {model: list(model.objects.values()) for model in models}
    state = accountant_control_dashboard_tasks(as_of=date(2026, 10, 13))
    forbid_side_effects(monkeypatch)
    monkeypatch.setattr("accounting_export.archive.archive_accountant_report_pdf", lambda *a, **k: pytest.fail("Archive"))
    monkeypatch.setattr(Path, "open", lambda *a, **k: pytest.fail("Filesystem"))
    with connection.execute_wrapper(read_only_export_execute):
        export = only_export(company)
        output = render_arbitrary_accountant_csv(export)
    assert before == {model: list(model.objects.values()) for model in models}
    assert state == accountant_control_dashboard_tasks(as_of=date(2026, 10, 13))
    Invoice.objects.filter(pk=item.pk).update(prejemnik_naziv="Edited after preparation")
    assert render_arbitrary_accountant_csv(export) == output


def test_query_count_constant_for_multiple_companies_and_invoices(company, django_assert_num_queries):
    companies = [company] + [Company.objects.create(naziv=f"Other {i}", davcna_stevilka=f"8765432{i}") for i in range(3)]
    for selected in companies:
        for index in range(3):
            invoice(selected, number=f"NP-{index}", issued=date(2026, 9, 15))
    with django_assert_num_queries(3):
        result = prepare(companies)
        for item in result.companies:
            assert item.export.issued_count == 3
            render_arbitrary_accountant_csv(item.export)


def test_monthly_csv_baseline_bytes_unchanged(report):
    from accounting_export.csv_renderer import (
        accountant_report_csv_filename,
        render_accountant_report_csv,
    )

    assert hashlib.sha256(render_accountant_report_csv(report)).hexdigest() == "9b82f297656f8a385ccdfe144d40bea6bdaae4f7653653693da6144570d310f8"
    assert accountant_report_csv_filename(report) == "2026-07-Ziva-Alfa-doo.csv"
