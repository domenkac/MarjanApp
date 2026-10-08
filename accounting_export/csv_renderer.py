"""Render accountant snapshots using the shared PRD control CSV schema."""

import csv
import json
import re
from datetime import date, datetime
from io import StringIO

from django.core.exceptions import ValidationError

from core.pravila import slugify_naziv

from .models import AccountantReport

_SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

_COLUMNS = (
    "podjetje", "podjetje_davcna_stevilka", "podjetje_maticna_stevilka",
    "obdobje_porocila", "datum_izdelave", "vrstica_tip", "stevilka_racuna",
    "tip", "prejemnik", "davcna_stevilka", "maticna_stevilka", "oznaka_pogodbe",
    "datum_izdaje", "obdobje_od", "obdobje_do", "ddv_rezim", "osnova", "ddv",
    "skupaj", "razlog",
)
_RECIPIENT_FIELDS = (
    ("prejemnik", "recipient_name"),
    ("davcna_stevilka", "recipient_tax_number"),
    ("maticna_stevilka", "recipient_registration_number"),
    ("oznaka_pogodbe", "contract_reference"),
)


def _invalid(path: str) -> ValidationError:
    return ValidationError(f"Invalid accountant report snapshot: {path}.")


def _text(row: dict, field: str, path: str) -> str:
    value = row.get(field)
    if not isinstance(value, str):
        raise _invalid(f"{path}.{field}")
    return value


def _decimal(row: dict, field: str, path: str) -> str:
    value = _text(row, field, path)
    if not re.fullmatch(r"-?[0-9]+\.[0-9]{2}", value):
        raise _invalid(f"{path}.{field}")
    return value.replace(".", ",")


def _date(row: dict, field: str, path: str) -> str:
    value = _text(row, field, path)
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise _invalid(f"{path}.{field}") from exc
    return f"{parsed.day:02d}.{parsed.month:02d}.{parsed.year:04d}"


def _snapshot(report: AccountantReport) -> dict:
    # A deferred snapshot must fail rather than trigger a database read.
    snapshot = report.__dict__.get("snapshot")
    if not isinstance(snapshot, dict):
        raise _invalid("snapshot")
    version = snapshot.get("schema_version")
    if type(version) is not int or version not in _SUPPORTED_SCHEMA_VERSIONS:
        raise _invalid("schema_version (unsupported)")
    if not isinstance(snapshot.get("header"), dict):
        raise _invalid("header")
    for section in ("issued_rows", "recap_rows", "unissued_rows"):
        if not isinstance(snapshot.get(section), list):
            raise _invalid(section)
    return snapshot


def _metadata(header: dict) -> dict:
    period = _text(header, "period", "header")
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", period):
            raise ValueError
        date.fromisoformat(f"{period}-01")
    except ValueError as exc:
        raise _invalid("header.period") from exc
    return _metadata_for_period(header, period)


def _metadata_for_period(header: dict, period: str) -> dict:
    generated_at = _text(header, "generated_at", "header")
    try:
        if "T" not in generated_at:
            raise ValueError
        generated_datetime = datetime.fromisoformat(generated_at)
        if generated_datetime.utcoffset() is None:
            raise ValueError
        generated_date = generated_datetime.date()
    except ValueError as exc:
        raise _invalid("header.generated_at") from exc
    return {
        "podjetje": _text(header, "company_name", "header"),
        "podjetje_davcna_stevilka": _text(header, "company_tax_number", "header"),
        "podjetje_maticna_stevilka": _text(header, "company_registration_number", "header"),
        "obdobje_porocila": period,
        # Use the stored timestamp's date, without a mutable timezone setting.
        "datum_izdelave": (
            f"{generated_date.day:02d}.{generated_date.month:02d}.{generated_date.year:04d}"
        ),
    }


def _vat_display(row: dict, path: str) -> str:
    regimes = row.get("vat_regimes")
    if not isinstance(regimes, list) or not regimes:
        raise _invalid(f"{path}.vat_regimes")
    displays = []
    for index, regime in enumerate(regimes):
        regime_path = f"{path}.vat_regimes[{index}]"
        if not isinstance(regime, dict):
            raise _invalid(regime_path)
        method = _text(regime, "vat_method", regime_path)
        rate = _decimal(regime, "vat_rate", regime_path)
        group = _text(regime, "vat_summary_group", regime_path)
        displays.append(f"{method} {rate} % [{group}]")
    return " | ".join(displays)


def render_accountant_report_csv(report: AccountantReport) -> bytes:
    """Return BOM-prefixed UTF-8 CSV, reading only the loaded immutable snapshot."""
    snapshot = _snapshot(report)
    metadata = _metadata(snapshot["header"])
    return _render_rows(snapshot, metadata)


def render_arbitrary_accountant_csv(export) -> bytes:
    """Render an in-memory export through the same schema and row serializer."""
    snapshot = json.loads(export.snapshot_json)
    metadata = _metadata_for_period(snapshot["header"], export.period_value)
    return _render_rows(snapshot, metadata)


def _render_rows(snapshot: dict, metadata: dict) -> bytes:
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=_COLUMNS, delimiter=";", lineterminator="\r\n")
    writer.writeheader()
    for section, kind in (
        ("issued_rows", "RACUN"),
        ("recap_rows", "REKAPITULACIJA"),
        ("unissued_rows", "NEIZDANO"),
    ):
        for index, row in enumerate(snapshot[section]):
            path = f"{section}[{index}]"
            if not isinstance(row, dict):
                raise _invalid(path)
            rendered = metadata | {"vrstica_tip": kind}
            if kind in ("RACUN", "NEIZDANO"):
                for column, field in _RECIPIENT_FIELDS:
                    rendered[column] = _text(row, field, path)
            if kind == "RACUN":
                rendered["stevilka_racuna"] = _text(row, "invoice_number", path)
                rendered["tip"] = _text(row, "document_type", path)
                for column, field in (
                    ("datum_izdaje", "issue_date"),
                    ("obdobje_od", "service_period_start"),
                    ("obdobje_do", "service_period_end"),
                ):
                    rendered[column] = _date(row, field, path)
                rendered["ddv_rezim"] = _vat_display(row, path)
            elif kind == "REKAPITULACIJA":
                group = _text(row, "vat_summary_group", path)
                rate = _decimal(row, "vat_rate", path)
                rendered["ddv_rezim"] = f"{group} / {rate} %"
            else:
                rendered["razlog"] = _text(row, "reason", path)
            if kind in ("RACUN", "REKAPITULACIJA"):
                for column, field in (
                    ("osnova", "net_amount"), ("ddv", "vat_amount"), ("skupaj", "gross_amount"),
                ):
                    rendered[column] = _decimal(row, field, path)
            writer.writerow(rendered)
    return output.getvalue().encode("utf-8-sig")


def accountant_report_csv_filename(report: AccountantReport) -> str:
    """Return the monthly filename from the snapshot's historical header."""
    metadata = _metadata(_snapshot(report)["header"])
    return f"{metadata['obdobje_porocila']}-{slugify_naziv(metadata['podjetje'])}.csv"
