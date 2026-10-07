import codecs
import csv
import importlib
from copy import deepcopy
from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

import accounting_export.services as report_services
from accounting_export import csv_renderer
from accounting_export.csv_renderer import (
    accountant_report_csv_filename,
    render_accountant_report_csv,
)
from accounting_export.models import AccountantReport

HEADER = (
    "podjetje;podjetje_davcna_stevilka;podjetje_maticna_stevilka;"
    "obdobje_porocila;datum_izdelave;vrstica_tip;stevilka_racuna;tip;"
    "prejemnik;davcna_stevilka;maticna_stevilka;oznaka_pogodbe;"
    "datum_izdaje;obdobje_od;obdobje_do;ddv_rezim;osnova;ddv;skupaj;razlog"
)


@pytest.fixture
def report():
    return AccountantReport(snapshot={
        "schema_version": 1,
        "header": {
            "company_name": "Živa Alfa d.o.o.",
            "company_tax_number": "15012557",
            "company_registration_number": "1234567000",
            "period": "2026-07",
            "generated_at": "2026-08-02T01:30:00.123456+02:00",
        },
        "issued_rows": [{
            "invoice_number": "NP-00001",
            "document_type": "NP",
            "recipient_name": "Špela Černe",
            "recipient_tax_number": "87654321",
            "recipient_registration_number": "7654321000",
            "contract_reference": "P-1",
            "issue_date": "2026-07-05",
            "service_period_start": "2026-07-01",
            "service_period_end": "2026-07-31",
            "vat_regimes": [{
                "vat_method": "OBDAVCENO", "vat_rate": "22.00",
                "vat_summary_group": "OBD_22", "vat_clause": "Not exported",
            }],
            "net_amount": "1200.00", "vat_amount": "264.00",
            "gross_amount": "1464.00",
        }],
        "recap_rows": [{
            "vat_summary_group": "OBD_22", "vat_rate": "22.00",
            "net_amount": "1200.00", "vat_amount": "264.00",
            "gross_amount": "1464.00",
        }],
        "unissued_rows": [{
            "recipient_name": "Neizdani najemnik",
            "recipient_tax_number": "", "recipient_registration_number": "",
            "contract_reference": "P-NE", "reason": "Ni obračunljivih postavk",
            "reason_source": "SYSTEM",
        }],
    })


def _rows(report):
    return list(csv.reader(
        StringIO(render_accountant_report_csv(report).decode("utf-8-sig"), newline=""),
        delimiter=";",
    ))


def test_exact_format_and_all_row_mappings(report):
    output = render_accountant_report_csv(report)
    assert output.startswith(codecs.BOM_UTF8)
    text = output.decode("utf-8-sig")
    assert text.split("\r\n")[0] == HEADER
    assert text.endswith("\r\n")
    assert "\n" not in text.replace("\r\n", "")
    metadata = ["Živa Alfa d.o.o.", "15012557", "1234567000", "2026-07", "02.08.2026"]
    rows = _rows(report)
    assert rows[0] == HEADER.split(";")
    assert all(len(row) == 20 for row in rows)
    assert rows[1:] == [
        metadata + [
            "RACUN", "NP-00001", "NP", "Špela Černe", "87654321", "7654321000",
            "P-1", "05.07.2026", "01.07.2026", "31.07.2026",
            "OBDAVCENO 22,00 % [OBD_22]", "1200,00", "264,00", "1464,00", "",
        ],
        metadata + ["REKAPITULACIJA"] + [""] * 9 + [
            "OBD_22 / 22,00 %", "1200,00", "264,00", "1464,00", "",
        ],
        metadata + [
            "NEIZDANO", "", "", "Neizdani najemnik", "", "", "P-NE",
        ] + [""] * 7 + ["Ni obračunljivih postavk"],
    ]


@pytest.mark.parametrize("timestamp,expected_date", [
    ("2026-08-02T01:30:00+02:00", "02.08.2026"),
    ("2026-01-16T00:30:00+01:00", "16.01.2026"),
])
def test_creation_date_uses_persisted_local_offset_at_midnight_boundary(
    report, timestamp, expected_date
):
    report.snapshot["header"]["generated_at"] = timestamp
    with timezone.override("UTC"):
        first = render_accountant_report_csv(report)
        assert all(row[4] == expected_date for row in _rows(report)[1:])
    with timezone.override("America/New_York"):
        assert render_accountant_report_csv(report) == first


@pytest.mark.parametrize("value", ['Špela;Černe', 'Špela "Černe"', "Špela\nČerne", "Špela\r\nČerne"])
def test_csv_quotes_and_round_trips_text(report, value):
    report.snapshot["issued_rows"][0]["recipient_name"] = value
    assert _rows(report)[1][8] == value
    assert '"' in render_accountant_report_csv(report).decode("utf-8-sig").split("\r\n", 1)[1]


def test_mixed_vat_remains_one_invoice_with_snapshot_regime_order(report):
    invoice = report.snapshot["issued_rows"][0]
    invoice["vat_regimes"] = [
        {"vat_method": "OPROSCENO", "vat_rate": "0.00", "vat_summary_group": "OPR_44"},
        invoice["vat_regimes"][0],
        {"vat_method": "NI_PREDMET", "vat_rate": "0.00", "vat_summary_group": "NI_PREDMET"},
    ]
    rows = _rows(report)[1:]
    issued = [row for row in rows if row[5] == "RACUN"]
    assert len(issued) == 1
    assert issued[0][15] == (
        "OPROSCENO 0,00 % [OPR_44] | OBDAVCENO 22,00 % [OBD_22] | "
        "NI_PREDMET 0,00 % [NI_PREDMET]"
    )
    assert issued[0][16:19] == ["1200,00", "264,00", "1464,00"]
    assert [row[15] for row in rows if row[5] == "REKAPITULACIJA"] == ["OBD_22 / 22,00 %"]
    report.snapshot["recap_rows"] = []
    assert not any(row[5] == "REKAPITULACIJA" for row in _rows(report)[1:])


@pytest.mark.parametrize("kind", ["STORNO", "DOBROPIS"])
def test_negative_and_zero_amounts_without_thousands_separator(report, kind):
    invoice = report.snapshot["issued_rows"][0]
    invoice.update(document_type=kind, net_amount="-125.50", vat_amount="0.00", gross_amount="-125.50")
    assert _rows(report)[1][7] == kind
    assert _rows(report)[1][16:19] == ["-125,50", "0,00", "-125,50"]


def test_section_and_list_order_is_preserved(report):
    for section, field in [
        ("issued_rows", "invoice_number"), ("recap_rows", "vat_summary_group"),
        ("unissued_rows", "contract_reference"),
    ]:
        second = deepcopy(report.snapshot[section][0])
        report.snapshot[section][0][field] = "Z"
        second[field] = "A"
        report.snapshot[section].append(second)
    rows = _rows(report)[1:]
    assert [row[5] for row in rows] == ["RACUN"] * 2 + ["REKAPITULACIJA"] * 2 + ["NEIZDANO"] * 2
    assert [row[6] for row in rows[:2]] == ["Z", "A"]
    assert [row[15] for row in rows[2:4]] == ["Z / 22,00 %", "A / 22,00 %"]
    assert [row[11] for row in rows[4:]] == ["Z", "A"]


def test_pure_deterministic_render_and_historical_filename(report):
    before = deepcopy(report.snapshot)
    report.company_id = 999
    report.period = "2099-12"
    report.generated_at = "2099-12-31"
    report.schema_version = 999
    first = render_accountant_report_csv(report)
    assert render_accountant_report_csv(report) == first
    assert accountant_report_csv_filename(report) == "2026-07-Ziva-Alfa-doo.csv"
    assert report.snapshot == before


@pytest.mark.django_db
def test_both_functions_execute_zero_queries(report, django_assert_num_queries):
    report.company_id = 999
    with django_assert_num_queries(0):
        render_accountant_report_csv(report)
        accountant_report_csv_filename(report)


@pytest.mark.parametrize("renderer", [render_accountant_report_csv, accountant_report_csv_filename])
def test_historical_schema_support_is_independent_of_writer_version(report, monkeypatch, renderer):
    expected = renderer(report)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(report_services, "SCHEMA_VERSION", 2)
            # Reload to catch import-time coupling to the current writer version.
            importlib.reload(csv_renderer)
            assert renderer(report) == expected
            report.snapshot["schema_version"] = 2
            with pytest.raises(ValidationError, match="schema_version"):
                renderer(report)
    finally:
        importlib.reload(csv_renderer)


@pytest.mark.parametrize("renderer", [render_accountant_report_csv, accountant_report_csv_filename])
def test_naive_creation_timestamp_fails(report, renderer):
    report.snapshot["header"]["generated_at"] = "2026-08-02T01:30:00"
    with pytest.raises(ValidationError, match="header.generated_at"):
        renderer(report)


@pytest.mark.parametrize("version", [2, None, "1", True])
@pytest.mark.parametrize("renderer", [render_accountant_report_csv, accountant_report_csv_filename])
def test_unsupported_schema_fails(report, version, renderer):
    report.snapshot["schema_version"] = version
    with pytest.raises(ValidationError, match="schema_version"):
        renderer(report)


@pytest.mark.parametrize("section", ["header", "issued_rows", "recap_rows", "unissued_rows"])
@pytest.mark.parametrize("invalid", [None, "bad", 42])
def test_malformed_sections_fail(report, section, invalid):
    report.snapshot[section] = invalid
    with pytest.raises(ValidationError, match=section):
        render_accountant_report_csv(report)


REQUIRED_FIELDS = {
    "header": ["company_name", "company_tax_number", "company_registration_number", "period", "generated_at"],
    "issued_rows": [
        "invoice_number", "document_type", "recipient_name", "recipient_tax_number",
        "recipient_registration_number", "contract_reference", "issue_date",
        "service_period_start", "service_period_end", "vat_regimes",
        "net_amount", "vat_amount", "gross_amount",
    ],
    "recap_rows": ["vat_summary_group", "vat_rate", "net_amount", "vat_amount", "gross_amount"],
    "unissued_rows": ["recipient_name", "recipient_tax_number", "recipient_registration_number", "contract_reference", "reason"],
}


@pytest.mark.parametrize("section,field", [
    (section, field) for section, fields in REQUIRED_FIELDS.items() for field in fields
])
@pytest.mark.parametrize("invalid", ["missing", None, 42])
def test_required_fields_fail_clearly(report, section, field, invalid):
    row = report.snapshot[section] if section == "header" else report.snapshot[section][0]
    if invalid == "missing":
        del row[field]
    else:
        row[field] = invalid
    with pytest.raises(ValidationError, match=field):
        render_accountant_report_csv(report)


@pytest.mark.parametrize("field", ["vat_method", "vat_rate", "vat_summary_group"])
def test_missing_vat_regime_fields_fail(report, field):
    del report.snapshot["issued_rows"][0]["vat_regimes"][0][field]
    with pytest.raises(ValidationError, match=field):
        render_accountant_report_csv(report)


@pytest.mark.parametrize("section,field,value", [
    ("header", "period", "2026-13"), ("header", "period", "2026-7"),
    ("header", "generated_at", "not-a-date"),
    ("issued_rows", "issue_date", "2026-02-30"),
    ("issued_rows", "net_amount", "NaN"), ("issued_rows", "net_amount", "1.234"),
    ("issued_rows", "net_amount", "1,000.00"),
    ("issued_rows", "vat_regimes", []), ("issued_rows", "vat_regimes", [None]),
    ("recap_rows", "vat_rate", "Infinity"),
])
def test_invalid_render_values_fail(report, section, field, value):
    row = report.snapshot[section] if section == "header" else report.snapshot[section][0]
    row[field] = value
    with pytest.raises(ValidationError):
        render_accountant_report_csv(report)


@pytest.mark.parametrize("snapshot", [None, [], {}, "bad"])
def test_invalid_snapshot_fails(report, snapshot):
    report.snapshot = snapshot
    with pytest.raises(ValidationError):
        render_accountant_report_csv(report)


@pytest.mark.parametrize("section", ["header", "issued_rows", "recap_rows", "unissued_rows"])
def test_missing_sections_fail(report, section):
    del report.snapshot[section]
    with pytest.raises(ValidationError, match=section):
        render_accountant_report_csv(report)
