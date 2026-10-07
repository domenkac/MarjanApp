"""Snapshot-only presentation and self-contained accountant control PDF."""

import re
from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.db import connection
from django.template.loader import render_to_string
from weasyprint import HTML

from .models import AccountantReport

SUPPORTED_SCHEMA_VERSIONS = frozenset({1})


def _invalid(path):
    return ValidationError(f"Invalid accountant report snapshot: {path}.")


def _text(row, field, path):
    value = row.get(field)
    if not isinstance(value, str):
        raise _invalid(f"{path}.{field}")
    return value


def _amount(row, field, path):
    value = _text(row, field, path)
    if not re.fullmatch(r"-?[0-9]+\.[0-9]{2}", value):
        raise _invalid(f"{path}.{field}")
    return value.replace(".", ",")


def _date(row, field, path):
    value = _text(row, field, path)
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise _invalid(f"{path}.{field}") from exc
    return f"{parsed.day:02d}.{parsed.month:02d}.{parsed.year:04d}"


def prepare_accountant_report_context(report: AccountantReport) -> dict:
    """Validate renderer schema v1; never fetch deferred or live business data."""
    snapshot = report.__dict__.get("snapshot")
    if not isinstance(snapshot, dict):
        raise _invalid("snapshot")
    version = snapshot.get("schema_version")
    if type(version) is not int or version not in SUPPORTED_SCHEMA_VERSIONS:
        raise _invalid("schema_version (unsupported)")
    header = snapshot.get("header")
    if not isinstance(header, dict):
        raise _invalid("header")
    context = {field: _text(header, field, "header") for field in (
        "company_name", "company_tax_number", "company_registration_number", "period",
    )}
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", context["period"]):
            raise ValueError
        date.fromisoformat(context["period"] + "-01")
    except ValueError as exc:
        raise _invalid("header.period") from exc
    timestamp = _text(header, "generated_at", "header")
    try:
        if "T" not in timestamp:
            raise ValueError
        generated = datetime.fromisoformat(timestamp)
        if generated.utcoffset() is None:
            raise ValueError
    except ValueError as exc:
        raise _invalid("header.generated_at") from exc
    context["generated_date"] = _date(
        {"date": generated.date().isoformat()}, "date", "header.generated_at"
    )
    for section in ("issued_rows", "recap_rows", "unissued_rows"):
        rows = snapshot.get(section)
        if not isinstance(rows, list):
            raise _invalid(section)
        prepared = []
        for index, row in enumerate(rows):
            path = f"{section}[{index}]"
            if not isinstance(row, dict):
                raise _invalid(path)
            output = {}
            if section != "recap_rows":
                for field in (
                    "recipient_name", "recipient_tax_number", "recipient_registration_number",
                    "contract_reference",
                ):
                    output[field] = _text(row, field, path)
            if section == "issued_rows":
                for field in ("invoice_number", "document_type"):
                    output[field] = _text(row, field, path)
                for field in ("issue_date", "service_period_start", "service_period_end"):
                    output[field] = _date(row, field, path)
                regimes = row.get("vat_regimes")
                if not isinstance(regimes, list) or not regimes:
                    raise _invalid(f"{path}.vat_regimes")
                displays = []
                for regime_index, regime in enumerate(regimes):
                    regime_path = f"{path}.vat_regimes[{regime_index}]"
                    if not isinstance(regime, dict):
                        raise _invalid(regime_path)
                    method = _text(regime, "vat_method", regime_path)
                    rate = _amount(regime, "vat_rate", regime_path)
                    group = _text(regime, "vat_summary_group", regime_path)
                    displays.append(f"{method} {rate} % [{group}]")
                output["vat_display"] = " | ".join(displays)
            elif section == "recap_rows":
                group = _text(row, "vat_summary_group", path)
                rate = _amount(row, "vat_rate", path)
                output["vat_display"] = f"{group} / {rate} %"
            else:
                output["reason"] = _text(row, "reason", path)
            if section != "unissued_rows":
                for field in ("net_amount", "vat_amount", "gross_amount"):
                    output[field] = _amount(row, field, path)
            prepared.append(output)
        context[section] = prepared
    return context


def prepare_accountant_report_html(report: AccountantReport) -> str:
    return render_to_string(
        "accounting_export/control_report.html", prepare_accountant_report_context(report)
    )


def _deny_resource(url):
    raise ValidationError("Accountant PDF resources are forbidden.")


def render_accountant_report_pdf(report: AccountantReport) -> bytes:
    """Return PDF bytes without database or filesystem writes."""
    if connection.in_atomic_block:
        raise ValidationError("PDF rendering cannot run inside a DB transaction.")
    pdf = HTML(string=prepare_accountant_report_html(report), url_fetcher=_deny_resource).write_pdf()
    if not pdf.startswith(b"%PDF-"):
        raise ValidationError("Accountant renderer returned invalid PDF bytes.")
    return pdf
