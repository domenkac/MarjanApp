from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from invoicing.models import Invoice, MonthlyBatch, MonthlyCandidateReview, MonthlyCycle
from invoicing.services import (
    CALCULATION_SNAPSHOT_VERSION,
    preveri_integriteto_zamrznjenega_kandidata,
    preveri_integriteto_zamrznjenega_mesecnega_posnetka,
)

from .models import AccountantReport

SCHEMA_VERSION = 1
_NON_ISSUED_STATUSES = {
    Invoice.Status.OSNUTEK,
    Invoice.Status.ZA_PONOVNI_PREGLED,
    Invoice.Status.ARHIVIRAN_NI_IZDAN,
}


def _money(value: Decimal) -> str:
    return format(value, ".2f")


def _next_month(period: date) -> date:
    return date(period.year + (period.month == 12), period.month % 12 + 1, 1)


def issued_invoice_queryset(*, company_ids, period, period_end=None):
    """Canonical issued-document selection, shared by snapshots and task checks."""
    return (
        Invoice.objects.filter(
            company_id__in=company_ids,
            datum_izdaje__gte=period,
            datum_izdaje__lt=period_end if period_end is not None else _next_month(period),
        )
        .exclude(stevilka__isnull=True)
        .exclude(stevilka="")
        .exclude(status__in=_NON_ISSUED_STATUSES)
        .order_by("datum_izdaje", "stevilka", "pk")
    )


def _issued_invoices(company, period):
    return list(
        issued_invoice_queryset(company_ids=[company.pk], period=period)
        .prefetch_related("invoiceline_set")
    )


def issued_snapshot_header(invoices, *, period_label, generated_at):
    """Build issuer metadata exclusively from one historical invoice identity."""
    if not invoices:
        raise ValidationError(
            "Accountant report has no issued invoice snapshot for its historical header."
        )
    identities = {
        (
            invoice.izdajatelj_naziv,
            invoice.izdajatelj_davcna_stevilka,
            invoice.izdajatelj_maticna_stevilka,
        )
        for invoice in invoices
    }
    if len(identities) != 1:
        raise ValidationError(
            "Issued invoice issuer identity snapshots conflict within the reporting month."
        )
    name, tax_number, registration_number = identities.pop()
    return {
        "company_name": name,
        "company_tax_number": tax_number,
        "company_registration_number": registration_number,
        "period": period_label,
        "generated_at": generated_at.isoformat(),
    }


def issued_and_recap_rows(invoices):
    """Build issued rows and VAT recap from invoice and prefetched line snapshots."""
    issued_rows = []
    recap = defaultdict(lambda: [Decimal("0.00"), Decimal("0.00"), Decimal("0.00")])
    for invoice in invoices:
        lines = list(invoice.invoiceline_set.all())
        net = sum((line.osnova for line in lines), Decimal("0.00"))
        vat = sum((line.ddv_znesek for line in lines), Decimal("0.00"))
        gross = sum((line.skupaj for line in lines), Decimal("0.00"))
        vat_regimes = sorted(
            {
                (
                    line.ddv_nacin_obracuna,
                    line.ddv_stopnja,
                    line.ddv_rekap_skupina,
                    line.ddv_klavzula,
                )
                for line in lines
            },
            key=lambda item: (item[0], item[1], item[2], item[3]),
        )
        issued_rows.append(
            {
                "invoice_number": invoice.stevilka,
                "document_type": invoice.document_type,
                "recipient_name": invoice.prejemnik_naziv,
                "recipient_tax_number": invoice.prejemnik_davcna_stevilka,
                "recipient_registration_number": invoice.prejemnik_maticna_stevilka,
                "contract_reference": invoice.oznaka_pogodbe or "",
                "issue_date": invoice.datum_izdaje.isoformat(),
                "service_period_start": invoice.obdobje_od.isoformat(),
                "service_period_end": invoice.obdobje_do.isoformat(),
                "vat_regimes": [
                    {
                        "vat_method": method,
                        "vat_rate": _money(rate),
                        "vat_summary_group": group,
                        "vat_clause": clause,
                    }
                    for method, rate, group, clause in vat_regimes
                ],
                "net_amount": _money(net),
                "vat_amount": _money(vat),
                "gross_amount": _money(gross),
            }
        )
        for line in lines:
            if line.ddv_nacin_obracuna == "NI_PREDMET":
                continue
            totals = recap[(line.ddv_rekap_skupina, line.ddv_stopnja)]
            totals[0] += line.osnova
            totals[1] += line.ddv_znesek
            totals[2] += line.skupaj
    recap_rows = [
        {
            "vat_summary_group": group,
            "vat_rate": _money(rate),
            "net_amount": _money(totals[0]),
            "vat_amount": _money(totals[1]),
            "gross_amount": _money(totals[2]),
        }
        for (group, rate), totals in sorted(recap.items())
    ]
    return issued_rows, recap_rows


def _completed_monthly_batch(company, period):
    batches = list(
        MonthlyBatch.objects.filter(company=company, cycle__obdobje=period)
        .select_related("cycle")
        .order_by("pk")
    )
    if len(batches) != 1:
        raise ValidationError(
            "Accountant report requires complete, unambiguous completed monthly evidence."
        )
    batch = batches[0]
    if (
        batch.cycle.status != MonthlyCycle.Status.ZAKLJUCEN
        or batch.cycle.trenutni_korak != MonthlyCycle.Korak.ZAKLJUCEK
        or batch.calculation_status != MonthlyBatch.CalculationStatus.CALCULATED
        or batch.calculation_schema_version != CALCULATION_SNAPSHOT_VERSION
    ):
        raise ValidationError(
            "Accountant report requires completed monthly evidence."
        )
    preveri_integriteto_zamrznjenega_mesecnega_posnetka(
        batch.calculation_snapshot, batch
    )
    return batch


def _unissued_rows(batch):
    rows = []
    snapshot = batch.calculation_snapshot
    identities = set()
    for candidate in snapshot.get("candidates", []):
        lease_id = candidate.get("lease_id")
        fingerprint = candidate.get("candidate_fingerprint")
        identity = (lease_id, fingerprint)
        if not lease_id or not fingerprint or identity in identities:
            raise ValidationError("Frozen monthly candidate is not uniquely identified.")
        identities.add(identity)
        reviews = MonthlyCandidateReview.objects.filter(
            batch=batch, lease_id=lease_id
        )
        try:
            review = reviews.get(candidate_fingerprint=fingerprint)
        except MonthlyCandidateReview.DoesNotExist as exc:
            message = (
                "Frozen candidate review fingerprint does not match."
                if reviews.exists()
                else "Frozen monthly candidate review is missing."
            )
            raise ValidationError(message) from exc
        prepared = preveri_integriteto_zamrznjenega_kandidata(
            snapshot, candidate, review
        )
        if review.selected:
            continue
        tenant = prepared["tenant"]
        rows.append(
            {
                "contract_reference": candidate["contract_reference"],
                "recipient_name": tenant["name"],
                "recipient_tax_number": tenant["tax_number"],
                "recipient_registration_number": tenant["registration_number"],
                "reason": prepared["reason"],
                "reason_source": (
                    "SYSTEM" if candidate["will_issue"] is not True else "USER"
                ),
            }
        )
    return sorted(rows, key=lambda row: (row["contract_reference"], row["recipient_name"]))


@transaction.atomic
def create_accountant_report(*, company, period: date) -> AccountantReport:
    if period.day != 1:
        raise ValidationError("Accountant report period must be the first day of a month.")
    generated_at = timezone.localtime(timezone.now())
    batch = _completed_monthly_batch(company, period)
    invoices = _issued_invoices(company, period)
    header = issued_snapshot_header(
        invoices, period_label=period.strftime("%Y-%m"), generated_at=generated_at
    )
    issued_rows, recap_rows = issued_and_recap_rows(invoices)
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "header": header,
        "issued_rows": issued_rows,
        "recap_rows": recap_rows,
        "unissued_rows": _unissued_rows(batch),
    }
    return AccountantReport.objects.create(
        company=company,
        period=period,
        generated_at=generated_at,
        schema_version=SCHEMA_VERSION,
        snapshot=snapshot,
    )
