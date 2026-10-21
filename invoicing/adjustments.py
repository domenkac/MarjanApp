"""Shared, transactional reservation and consumption for both NP issuance paths."""

import json
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import connection, models, transaction
from django.db.models import Q
from django.utils import timezone

from contracts.adjustments import (
    adjustment_approval_blockers,
    adjustment_decision_audit,
    adjustment_review_token,
    negative_authorization_reason_valid,
    review_adjustments,
)
from contracts.models import IndexationEvent
from core.models import AuditLogEntry
from core.pravila import zaokrozi

from .models import AdjustmentReservation, Invoice, InvoiceLine


def _require_transaction():
    if not connection.in_atomic_block:
        raise RuntimeError("Adjustment writes require an atomic transaction.")
    if connection.vendor == "sqlite" and connection.settings_dict["OPTIONS"].get(
        "transaction_mode"
    ) != "IMMEDIATE":
        raise RuntimeError("Adjustment writes require SQLite IMMEDIATE.")


def outstanding(lease_id, period_start):
    return review_adjustments().filter(
        Q(evidence_state="VERIFIED", znesek_poracuna__gt=0)
        | Q(evidence_state="MANUAL_REVIEW", znesek_poracuna__lt=0),
        indexation_event__lease_item__lease_id=lease_id,
        status="POTRJEN", vkljucen_v_racun__isnull=True,
        prizadeto_obdobje_do__lt=period_start,
    ).order_by("prizadeto_obdobje_od", "pk")


def evidence(row):
    negative = row.znesek_poracuna is not None and row.znesek_poracuna < 0
    blockers = adjustment_approval_blockers(row, authorize_negative=negative)
    if row.status != "POTRJEN":
        blockers.append("Poračun ni potrjen.")
    audit = adjustment_decision_audit(row)
    try:
        decision = json.loads(audit.razlog) if audit else {}
    except (ValueError, TypeError):
        decision = {}
    if (
        decision.get("event") != "INDEXATION_ADJUSTMENT_DECISION"
        or decision.get("action") != ("authorize_negative" if negative else "approve")
        or (negative and not negative_authorization_reason_valid(decision.get("reason")))
        or decision.get("decided_token") != adjustment_review_token(row)
    ):
        blockers.append("Potrditev poračuna ni več skladna z dokazili.")
    if blockers:
        raise ValidationError(blockers)
    return {
        "id": row.pk,
        "approval_token": decision["decided_token"],
        "approval_audit_id": audit.pk,
        "source_invoice_id": row.original_invoice_id,
        "source_line_id": row.source_line_id,
        "period_start": row.prizadeto_obdobje_od.isoformat(),
        "period_end": row.prizadeto_obdobje_do.isoformat(),
        "net": str(row.znesek_poracuna),
        "calculation_evidence": row.calculation_evidence,
        "observation_fingerprint": row.last_observation_fingerprint,
    }


def line_for(snapshot):
    stored = snapshot["calculation_evidence"]
    source = stored["source_line"]
    net = Decimal(snapshot["net"])
    start, end = date.fromisoformat(snapshot["period_start"]), date.fromisoformat(snapshot["period_end"])
    vat = (
        zaokrozi(net * Decimal(source["ddv_stopnja"]) / Decimal(100))
        if source["ddv_nacin_obracuna"] == "OBDAVCENO" else Decimal("0.00")
    )
    vat_fields = {
        name: source[name] for name in (
            "ddv_stopnja", "ddv_nacin_obracuna", "ddv_rezim_sifra",
            "ddv_zahteva_polni_odbitek", "ddv_klavzula",
            "ddv_rekap_skupina", "ddv_eslog_koda",
        )
    }
    vat_fields["ddv_stopnja"] = Decimal(vat_fields["ddv_stopnja"])
    return InvoiceLine(
        lease_id=stored["lease"]["id"], lease_item_id=source["lease_item"],
        adjustment_id=snapshot["id"], adjustment_snapshot=snapshot,
        opis=(
            f"Poračun indeksacije za obdobje {start:%d.%m.%Y} – {end:%d.%m.%Y}\n"
            f"Izvirnik {stored['source_invoice']['stevilka']}, vrstica #{snapshot['source_line_id']} · "
            f"Pogodba št. {stored['lease']['reference']}"
        ),
        osnova=net, ddv_znesek=vat, skupaj=net + vat, **vat_fields,
    )


def candidate_lines(lease_id, period_start, *, invoice_id=None):
    lines = []
    for row in outstanding(lease_id, period_start):
        snapshot = evidence(row)
        reservation = AdjustmentReservation.objects.filter(adjustment=row).first()
        if reservation and (invoice_id is None or reservation.invoice_id != invoice_id):
            raise ValidationError("Poračun je rezerviran za drug osnutek ali zamrznjeni kandidat.")
        lines.append(line_for(snapshot))
    return lines


def _audit(row, event, payload):
    AuditLogEntry.objects.create(
        content_type=ContentType.objects.get_for_model(row), object_id=row.pk,
        polje="vkljucen_v_racun" if event == "CONSUMED" else "reservation",
        stara_vrednost=str(payload["invoice_id"]) if event == "RELEASED" else "",
        nova_vrednost="" if event == "RELEASED" else str(payload.get("invoice_id") or payload.get("review_id")),
        razlog=json.dumps({"event": f"INDEXATION_ADJUSTMENT_{event}", **payload}, sort_keys=True),
    )


@transaction.atomic
def cancel_direct_draft(invoice, *, reason):
    """Delete an unissued direct draft and release its claims with durable audit receipts."""
    _require_transaction()
    current = Invoice.objects.filter(pk=invoice.pk).first()
    if current and (
        current.document_type != Invoice.DocumentType.NP
        or current.status != Invoice.Status.OSNUTEK or current.stevilka is not None
    ):
        raise ValueError("Račun ni neoštevilčen neposredni NP osnutek.")
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError("Razlog preklica osnutka je obvezen.")
    reason = reason.strip()
    invoice_type = ContentType.objects.get_for_model(Invoice)
    if current is None:
        audit = AuditLogEntry.objects.filter(
            content_type=invoice_type, object_id=invoice.pk,
            dejanje=AuditLogEntry.Dejanje.FIZICNI_IZBRIS,
        ).order_by("pk").last()
        try:
            receipt = json.loads(audit.razlog) if audit else {}
        except (ValueError, TypeError):
            receipt = {}
        if receipt.get("event") == "DIRECT_NP_DRAFT_CANCELLED" and receipt.get("reason") == reason:
            return False  # Exact retry must not touch a replacement owner's claims.
        raise ValidationError("Osnutek ne obstaja ali preklic nima skladnega revizijskega dokazila.")

    reservations = list(AdjustmentReservation.objects.filter(invoice=current).select_related("adjustment"))
    lines = list(current.invoiceline_set.all())
    adjustment_ids = {line.adjustment_id for line in lines if line.adjustment_id}
    if adjustment_ids != {row.adjustment_id for row in reservations} or any(
        line.adjustment_snapshot and not line.adjustment_id for line in lines
    ):
        raise ValidationError("Osnutek nima skladnih neposrednih rezervacij; zamrznjenih rezervacij ni dovoljeno sprostiti.")
    if any(
        row.review_id or row.consumed_invoice_id or row.consumed_at
        or row.adjustment.vkljucen_v_racun_id for row in reservations
    ):
        raise ValidationError("Porabljenih ali zamrznjenih rezervacij ni dovoljeno sprostiti.")

    invoice_id, company_id = current.pk, current.company_id
    for reservation in reservations:
        _audit(reservation.adjustment, "RELEASED", {
            "invoice_id": invoice_id, "reservation_id": reservation.pk,
            "reason": reason, "approval_token": reservation.evidence.get("approval_token"),
        })
        reservation.delete()
    current.invoiceline_set.all().delete()
    current.delete()
    audit = AuditLogEntry(
        content_type=invoice_type, object_id=invoice_id,
        dejanje=AuditLogEntry.Dejanje.FIZICNI_IZBRIS,
        razlog=json.dumps({
            "event": "DIRECT_NP_DRAFT_CANCELLED", "reason": reason,
            "company_id": company_id, "adjustment_ids": sorted(adjustment_ids),
        }, ensure_ascii=False, sort_keys=True),
    )
    audit.full_clean()
    audit.save()
    return True


@transaction.atomic
def reserve(snapshots, *, invoice=None, review=None):
    _require_transaction()
    if (invoice is None) == (review is None):
        raise ValidationError("Rezervacija zahteva natanko enega lastnika.")
    if invoice is not None:
        if invoice.document_type != "NP" or invoice.stevilka or invoice.status != "OSNUTEK":
            raise ValidationError("Rezervacija zahteva neoštevilčen NP osnutek.")
        lease_id, company_id, period_start = invoice.lease_id, invoice.company_id, invoice.obdobje_od
    else:
        if not review.selected or review.batch.calculation_schema_version != 5:
            raise ValidationError("Rezervacija zahteva izbrani pregled različice 5.")
        candidates = [candidate for candidate in review.batch.calculation_snapshot.get("candidates", [])
                      if candidate.get("lease_id") == review.lease_id
                      and candidate.get("candidate_fingerprint") == review.candidate_fingerprint]
        if len(candidates) != 1 or candidates[0].get("adjustments") != snapshots:
            raise ValidationError("Rezervacija se ne ujema s pregledanim kandidatom.")
        lease_id, company_id, period_start = review.lease_id, review.batch.company_id, review.batch.cycle.obdobje
    ids = [snapshot["id"] for snapshot in snapshots]
    if len(ids) != len(set(ids)):
        raise ValidationError("Podvojena identiteta poračuna.")
    for snapshot in snapshots:
        row = review_adjustments().get(pk=snapshot["id"])
        if (
            row.indexation_event.lease_item.lease_id != lease_id
            or row.original_invoice.company_id != company_id
            or row.prizadeto_obdobje_do >= period_start
        ):
            raise ValidationError("Lastnik rezervacije ni prihodnji NP iste pogodbe in podjetja.")
        if evidence(row) != snapshot:
            raise ValidationError("Dokazila poračuna so se po pregledu spremenila.")
        reservation, created = AdjustmentReservation.objects.get_or_create(
            adjustment=row,
            defaults={"invoice": invoice, "review": review, "evidence": snapshot},
        )
        if (
            reservation.invoice_id != (invoice.pk if invoice else None)
            or reservation.review_id != (review.pk if review else None)
            or reservation.evidence != snapshot or reservation.consumed_at
        ):
            raise ValidationError("Poračun je rezerviran za drugega lastnika.")
        if created:
            _audit(row, "RESERVED", {
                "invoice_id": reservation.invoice_id, "review_id": reservation.review_id,
                "approval_token": snapshot["approval_token"],
            })


def validate_indexed_rent(invoice, lines):
    """Block stale unissued rent after late approval, including frozen old packages."""
    from .services import _izracunaj_vrstice_np

    affected = set(IndexationEvent.objects.filter(
        lease_item__lease_id=invoice.lease_id, izkljucen=False,
        datum_uskladitve__lte=invoice.obdobje_do,
    ).values_list("lease_item_id", flat=True))
    if not affected:
        return
    actual = [(line.lease_item_id, line.osnova) for line in lines
              if not line.adjustment_id and line.lease_item_id in affected]
    expected = [(line.lease_item_id, line.osnova) for line in _izracunaj_vrstice_np(
        invoice.lease, invoice.obdobje_od, invoice.obdobje_do,
    ) if line.lease_item_id in affected]
    if sorted(actual) != sorted(expected):
        raise ValidationError("Najemnina ne uporablja potrjene indeksacije; osnutek ali paket je zastarel.")


def validate_before_numbering(invoice, lines, *, review=None):
    _require_transaction()
    regular = [line for line in lines if not line.adjustment_id]
    if any(bool(line.adjustment_snapshot) != bool(line.adjustment_id) for line in lines):
        raise ValidationError("Vrstica poračuna nima skladne identitete.")
    if not regular:
        raise ValidationError("Samostojen račun za poračun ni dovoljen.")
    if (any(line.adjustment_id and line.osnova < 0 for line in lines)
            and sum((line.skupaj for line in lines), Decimal("0.00")) <= 0):
        raise ValidationError("Negativni poračun zahteva pozitiven skupni znesek NP računa; potrebna je računovodska odločitev.")
    validate_indexed_rent(invoice, lines)
    rows = list(outstanding(invoice.lease_id, invoice.obdobje_od))
    snapshots = [evidence(row) for row in rows]
    adjustment_lines = [line for line in lines if line.adjustment_id]
    if {line.adjustment_id for line in adjustment_lines} != {row.pk for row in rows}:
        raise ValidationError("Manjka potrjen poračun ali vsebuje nedovoljen poračun; ponovite pregled.")
    if len(adjustment_lines) != len(rows):
        raise ValidationError("Podvojene vrstice poračuna.")
    owner = {"review_id": review.pk, "invoice__isnull": True} if review else {
        "invoice_id": invoice.pk, "review__isnull": True,
    }
    reservations = list(AdjustmentReservation.objects.filter(**owner))
    if {r.adjustment_id for r in reservations} != {row.pk for row in rows}:
        raise ValidationError("Rezervacije se ne ujemajo z računom.")
    for row, snapshot in zip(rows, snapshots, strict=True):
        reservation = next(r for r in reservations if r.adjustment_id == row.pk)
        if reservation.evidence != snapshot or reservation.consumed_at:
            raise ValidationError("Rezervacija je zastarela ali porabljena.")
        expected = line_for(snapshot)
        actual = next(line for line in adjustment_lines if line.adjustment_id == row.pk)
        for field in InvoiceLine._meta.concrete_fields:
            if field.name in {"id", "invoice"}:
                continue
            if getattr(expected, field.attname) != getattr(actual, field.attname):
                raise ValidationError("Vrstica poračuna se ne ujema s potrjenimi dokazili.")
        source = row.calculation_evidence["source_invoice"]
        if invoice.company_id != source["company"] or invoice.lease_id != source["lease"]:
            raise ValidationError("Poračun ne pripada podjetju ali pogodbi računa.")
        if any(getattr(invoice, field) != value for field, value in source.items()
               if field.startswith("prejemnik_")):
            raise ValidationError("Prejemnik poračuna se ne ujema z računom.")
        if any(getattr(invoice, field) != value for field, value in source.items()
               if field in {
                   "izdajatelj_naziv", "izdajatelj_naslov", "izdajatelj_drzava_koda",
                   "izdajatelj_identificiran_za_ddv", "izdajatelj_davcna_stevilka",
                   "izdajatelj_maticna_stevilka",
               } and value not in (None, "")):
            raise ValidationError("Izdajatelj poračuna se ne ujema z računom.")
    return reservations


def consume(invoice, reservations):
    _require_transaction()
    if not invoice.stevilka or invoice.document_type != "NP":
        raise ValidationError("Poraba zahteva oštevilčen NP račun.")
    reservations = list(reservations)
    review = reservations[0].review if reservations and reservations[0].review_id else None
    if review and (
        review.batch.cycle.trenutni_korak != 5 or not review.selected
        or review.lease_id != invoice.lease_id or review.batch.company_id != invoice.company_id
        or review.batch.cycle.obdobje != invoice.obdobje_od
    ):
        raise ValidationError("Poraba zahteva izbrani zamrznjeni kandidat istega računa.")
    fresh = validate_before_numbering(invoice, list(invoice.invoiceline_set.all()), review=review)
    if {row.pk for row in fresh} != {row.pk for row in reservations}:
        raise ValidationError("Poraba nima skladnih rezervacij.")
    reservations = fresh
    lines = list(invoice.invoiceline_set.all())
    if (
        invoice.osnova_skupaj != sum((line.osnova for line in lines), Decimal("0.00"))
        or invoice.ddv_skupaj != sum((line.ddv_znesek for line in lines), Decimal("0.00"))
        or invoice.skupaj_z_ddv != sum((line.skupaj for line in lines), Decimal("0.00"))
    ):
        raise ValidationError("Vsote računa niso skladne pred porabo poračunov.")
    for reservation in reservations:
        row = review_adjustments().get(pk=reservation.adjustment_id)
        if row.vkljucen_v_racun_id or reservation.consumed_at:
            raise ValidationError("Poračun je že porabljen.")
        if not InvoiceLine.objects.filter(invoice=invoice, adjustment=row).exists():
            raise ValidationError("Manjka posnetna vrstica poračuna.")
        row.vkljucen_v_racun = invoice
        models.Model.save(row, update_fields=["vkljucen_v_racun"])
        reservation.consumed_invoice = invoice
        reservation.consumed_at = timezone.now()
        reservation.save(update_fields=["consumed_invoice", "consumed_at"])
        _audit(row, "CONSUMED", {"invoice_id": invoice.pk, "reservation_id": reservation.pk})


def validate_consumed(invoice, lines, *, review=None):
    """Issued retries inspect stored identity only; never refresh historical evidence."""
    owner = {"review_id": review.pk} if review else {"invoice_id": invoice.pk}
    reservations = list(AdjustmentReservation.objects.filter(**owner))
    ids = {line.adjustment_id for line in lines if line.adjustment_id}
    if ids != {r.adjustment_id for r in reservations}:
        raise ValidationError("Izdani račun nima skladnih rezervacij.")
    for reservation in reservations:
        line = next(line for line in lines if line.adjustment_id == reservation.adjustment_id)
        if (
            reservation.consumed_invoice_id != invoice.pk or not reservation.consumed_at
            or reservation.adjustment.vkljucen_v_racun_id != invoice.pk
            or line.adjustment_snapshot != reservation.evidence
        ):
            raise ValidationError("Izdani poračun nima skladnega dokazila porabe.")
        expected = line_for(reservation.evidence)
        if any(
            getattr(line, field.attname) != getattr(expected, field.attname)
            for field in InvoiceLine._meta.concrete_fields if field.name not in {"id", "invoice"}
        ):
            raise ValidationError("Izdana vrstica se ne ujema s shranjenim dokazilom porabe.")
