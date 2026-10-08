"""Read-only accountant exports from immutable issued invoice snapshots."""

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

from companies.models import Company
from core.pravila import slugify_naziv

from .csv_renderer import render_arbitrary_accountant_csv
from .services import (
    issued_and_recap_rows,
    issued_invoice_queryset,
    issued_snapshot_header,
)


def validate_arbitrary_date_range(date_from, date_to) -> date:
    """Validate inclusive calendar dates and return the exclusive selector end."""
    if type(date_from) is not date or type(date_to) is not date:
        raise ValidationError("Vnesite veljavna datuma.")
    if date_from > date_to:
        raise ValidationError("Datum Od mora biti pred datumom Do ali enak.")
    try:
        return date_to + timedelta(days=1)
    except OverflowError as exc:
        raise ValidationError("Datum Do je zunaj podprtega območja.") from exc


@dataclass(frozen=True)
class ArbitraryCompanyExport:
    """An immutable export, including a serialized in-memory render payload."""

    company_id: int
    company_label: str
    date_from: date
    date_to: date
    generated_at: datetime
    issued_count: int
    snapshot_json: str
    filename: str

    @property
    def period_value(self) -> str:
        return f"{self.date_from.isoformat()}_{self.date_to.isoformat()}"


@dataclass(frozen=True)
class ArbitraryCompanyResult:
    company_id: int
    company_label: str
    code: str
    message: str
    export: ArbitraryCompanyExport | None = None


@dataclass(frozen=True)
class ArbitraryExportResult:
    generated_at: datetime
    companies: tuple[ArbitraryCompanyResult, ...]


def prepare_arbitrary_accountant_exports(
    *, date_from, date_to, company_ids, generated_at=None,
) -> ArbitraryExportResult:
    """Prepare independent company results without monthly evidence or side effects."""
    period_end = validate_arbitrary_date_range(date_from, date_to)
    if not isinstance(company_ids, (list, tuple)) or not company_ids or any(
        type(value) is not int or not 0 < value <= 2**63 - 1 for value in company_ids
    ):
        raise ValidationError("Izberite vsaj eno veljavno podjetje.")
    company_ids = tuple(dict.fromkeys(company_ids))
    if generated_at is None:
        generated_at = timezone.now()
    if not isinstance(generated_at, datetime) or timezone.is_naive(generated_at):
        raise ValidationError("Datum izdelave mora vsebovati časovni pas.")
    generated_at = timezone.localtime(generated_at)
    companies = Company.objects.in_bulk(company_ids)
    if len(companies) != len(company_ids):
        raise ValidationError("Izberite veljavna podjetja.")
    grouped = defaultdict(list)
    for invoice in issued_invoice_queryset(
        company_ids=company_ids, period=date_from, period_end=period_end,
    ).prefetch_related("invoiceline_set"):
        grouped[invoice.company_id].append(invoice)
    results = []
    period_label = f"{date_from.isoformat()}_{date_to.isoformat()}"
    for company_id in company_ids:
        # Live labels identify rejected selections only; never render historical data.
        label = companies[company_id].naziv
        invoices = grouped[company_id]
        if not invoices:
            results.append(ArbitraryCompanyResult(
                company_id, label, "NO_ISSUED_DOCUMENTS",
                "V izbranem obdobju ni izdanih dokumentov.",
            ))
            continue
        try:
            header = issued_snapshot_header(
                invoices, period_label=period_label, generated_at=generated_at,
            )
        except ValidationError:
            results.append(ArbitraryCompanyResult(
                company_id, label, "ISSUER_CONFLICT",
                "Podatki izdajatelja v izbranem obdobju niso enotni. Izpis ni mogoč.",
            ))
            continue
        # Existing slugify preserves punctuation; restrict the resulting filename
        # component to ASCII letters, digits, underscores and hyphens.
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", slugify_naziv(header["company_name"]))
        slug = slug.strip("-_") or "podjetje"
        try:
            issued_rows, recap_rows = issued_and_recap_rows(invoices)
            snapshot = {
                "header": header, "issued_rows": issued_rows,
                "recap_rows": recap_rows, "unissued_rows": [],
            }
            export = ArbitraryCompanyExport(
                company_id, header["company_name"], date_from, date_to, generated_at,
                len(issued_rows), json.dumps(snapshot, ensure_ascii=False),
                f"{period_label}-{slug}.csv",
            )
            # Validate each payload here so invalid siblings remain isolated.
            render_arbitrary_accountant_csv(export)
        except ValidationError:
            results.append(ArbitraryCompanyResult(
                company_id, label, "INVALID_DOCUMENTS",
                "Podatkov izdanih dokumentov ni mogoče varno izvoziti.",
            ))
            continue
        results.append(ArbitraryCompanyResult(company_id, export.company_label, "READY", "", export))
    return ArbitraryExportResult(generated_at, tuple(results))
