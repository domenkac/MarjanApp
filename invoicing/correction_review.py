"""State binding for the operational correction review; no financial rules."""

import hashlib
import json
from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.serializers.json import DjangoJSONEncoder

from companies.models import Company, Signatory
from core.models import AuditLogEntry
from invoicing.models import Invoice, InvoiceLine


def correction_review_snapshot(invoice):
    """Read saved lines and display totals without changing financial snapshots."""
    lines = list(invoice.invoiceline_set.order_by("pk"))
    original = invoice.povezan_racun
    if invoice.stevilka:
        totals = {"osnova": invoice.osnova_skupaj, "ddv_znesek": invoice.ddv_skupaj,
                  "skupaj": invoice.skupaj_z_ddv}
    else:
        totals = {name: sum((getattr(line, name) for line in lines), Decimal("0.00"))
                  for name in ("osnova", "ddv_znesek", "skupaj")}
    return {
        "invoice": invoice, "lines": lines, "totals": totals, "original": original,
        "original_lines": list(original.invoiceline_set.order_by("pk")),
    }


def correction_fingerprint(invoice):
    """Call within IMMEDIATE when comparing this state with issuance."""
    current = Invoice.objects.values().get(pk=invoice.pk)
    original_id = current["povezan_racun_id"]
    payload = {
        "version": 1,
        "templates": [settings.PDF_TEMPLATE_VERSION, settings.ST_PDF_TEMPLATE_VERSION],
        "feature_eslog": settings.FEATURE_ESLOG,
        "invoice": current,
        "lines": list(invoice.invoiceline_set.order_by("pk").values()),
        "original": Invoice.objects.values().get(pk=original_id),
        "original_lines": list(InvoiceLine.objects.filter(invoice_id=original_id).order_by("pk").values()),
        "company": Company.objects.values().get(pk=current["company_id"]),
        "signatories": list(Signatory.objects.filter(company_id=current["company_id"]).order_by("pk").values()),
        "vat_evidence": list(AuditLogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(InvoiceLine),
            object_id__in=invoice.invoiceline_set.values("pk"), polje="credit_line_source_v1",
        ).order_by("pk").values()),
    }
    encoded = json.dumps(payload, cls=DjangoJSONEncoder, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
