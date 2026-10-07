from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction

from accounting_export.pdf_renderer import (
    prepare_accountant_report_html,
    render_accountant_report_pdf,
)
from tests.accounting_export.test_csv_renderer import (  # noqa: F401
    REQUIRED_FIELDS,
    report,
)


def test_historical_content_and_purity(report):  # noqa: F811
    report.snapshot["issued_rows"][0]["vat_regimes"].append({
        "vat_method": "NI_PREDMET", "vat_rate": "0.00", "vat_summary_group": "NI_PREDMET",
    })
    report.snapshot["issued_rows"][0]["net_amount"] = "-1200.00"
    before = deepcopy(report.snapshot)
    report.company_id = 999
    report.schema_version = 999
    html = prepare_accountant_report_html(report)
    for value in (
        "Živa Alfa d.o.o.", "15012557", "1234567000", "2026-07", "02.08.2026",
        "NP-00001", "Špela Černe", "87654321", "7654321000", "P-1",
        "05.07.2026", "01.07.2026", "31.07.2026", "-1200,00", "264,00", "1464,00",
        "OBDAVCENO 22,00 % [OBD_22] | NI_PREDMET 0,00 % [NI_PREDMET]",
        "OBD_22 / 22,00 %", "Neizdani najemnik", "P-NE", "Ni obračunljivih postavk",
    ):
        assert value in html
    assert "NI_PREDMET /" not in html
    report.snapshot["recap_rows"] = []
    assert "OBD_22 /" not in prepare_accountant_report_html(report)
    report.snapshot["recap_rows"] = before["recap_rows"]
    assert report.snapshot == before


def test_pdf_and_escaping(report):  # noqa: F811
    report.snapshot["header"]["company_name"] = '<img src="https://example.com">Živa'
    assert "&lt;img" in prepare_accountant_report_html(report)
    assert render_accountant_report_pdf(report).startswith(b"%PDF-")


@pytest.mark.django_db(transaction=True)
def test_no_queries_and_no_render_inside_transaction(report, django_assert_num_queries):  # noqa: F811
    with django_assert_num_queries(0):
        prepare_accountant_report_html(report)
        render_accountant_report_pdf(report)
    with transaction.atomic(), pytest.raises(ValidationError, match="transaction"):
        render_accountant_report_pdf(report)


@pytest.mark.parametrize("section,field", [
    (section, field) for section, fields in REQUIRED_FIELDS.items() for field in fields
])
@pytest.mark.parametrize("value", [None, 42])
def test_required_values_fail(report, section, field, value):  # noqa: F811
    row = report.snapshot[section] if section == "header" else report.snapshot[section][0]
    row[field] = value
    with pytest.raises(ValidationError, match=field):
        prepare_accountant_report_html(report)


@pytest.mark.parametrize("section", ["header", "issued_rows", "recap_rows", "unissued_rows"])
@pytest.mark.parametrize("value", [None, "bad", 42])
def test_sections_fail(report, section, value):  # noqa: F811
    report.snapshot[section] = value
    with pytest.raises(ValidationError):
        render_accountant_report_pdf(report)


@pytest.mark.parametrize("section,field,value", [
    ("header", "generated_at", "2026-08-02T01:30:00"),
    ("header", "period", "2026-13"),
    ("issued_rows", "issue_date", "2026-02-30"),
    ("issued_rows", "net_amount", "NaN"),
    ("issued_rows", "net_amount", 1.5),
    ("issued_rows", "vat_regimes", []),
    ("issued_rows", "vat_regimes", [None]),
    ("recap_rows", "vat_rate", "Infinity"),
])
def test_invalid_values_fail(report, section, field, value):  # noqa: F811
    row = report.snapshot[section] if section == "header" else report.snapshot[section][0]
    row[field] = value
    with pytest.raises(ValidationError):
        prepare_accountant_report_html(report)


@pytest.mark.parametrize("version", [2, "1", True, None])
def test_schema_support(report, version, monkeypatch):  # noqa: F811
    monkeypatch.setattr("accounting_export.services.SCHEMA_VERSION", 2)
    assert "2026-07" in prepare_accountant_report_html(report)
    report.snapshot["schema_version"] = version
    with pytest.raises(ValidationError, match="schema_version"):
        render_accountant_report_pdf(report)


@pytest.mark.parametrize("section,field", [
    (section, field) for section, fields in REQUIRED_FIELDS.items() for field in fields
])
def test_missing_required_values_fail(report, section, field):  # noqa: F811
    row = report.snapshot[section] if section == "header" else report.snapshot[section][0]
    del row[field]
    with pytest.raises(ValidationError, match=field):
        prepare_accountant_report_html(report)


@pytest.mark.parametrize("section", ["header", "issued_rows", "recap_rows", "unissued_rows"])
def test_missing_sections_fail(report, section):  # noqa: F811
    del report.snapshot[section]
    with pytest.raises(ValidationError, match=section):
        prepare_accountant_report_html(report)


@pytest.mark.parametrize("field", ["vat_method", "vat_rate", "vat_summary_group"])
def test_missing_regime_values_fail(report, field):  # noqa: F811
    del report.snapshot["issued_rows"][0]["vat_regimes"][0][field]
    with pytest.raises(ValidationError, match=field):
        prepare_accountant_report_html(report)


def test_recap_unissued_and_order_are_persisted(report):  # noqa: F811
    from accounting_export.pdf_renderer import prepare_accountant_report_context
    report.snapshot["recap_rows"][0]["net_amount"] = "-9.50"
    report.snapshot["unissued_rows"][0].update(
        recipient_tax_number="12345678", recipient_registration_number="9876543210",
    )
    second = deepcopy(report.snapshot["recap_rows"][0])
    second["vat_summary_group"] = "OPR_44"
    report.snapshot["recap_rows"].append(second)
    context = prepare_accountant_report_context(report)
    assert [row["vat_display"] for row in context["recap_rows"]] == [
        "OBD_22 / 22,00 %", "OPR_44 / 22,00 %",
    ]
    assert context["recap_rows"][0]["net_amount"] == "-9,50"
    html = prepare_accountant_report_html(report)
    assert "9876543210" in html and "12345678" in html


@pytest.mark.parametrize("snapshot", [None, [], {}, "bad"])
def test_invalid_snapshot_fails(report, snapshot):  # noqa: F811
    report.snapshot = snapshot
    with pytest.raises(ValidationError):
        render_accountant_report_pdf(report)
