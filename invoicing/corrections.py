"""Historical VAT provenance and PDF-only corrections of standalone ST invoices."""

import hashlib
import json
import re
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder

from contracts.models import Tenant
from contracts.vat_eligibility import preveri_upravicenost_prejemnika
from core.models import AuditLogEntry
from core.pravila import izracunaj_iz_bruto

from .models import Invoice, InvoiceLine

VAT_FIELDS = (
    "ddv_stopnja", "ddv_nacin_obracuna", "ddv_eslog_koda", "ddv_rekap_skupina", "ddv_klavzula",
)
IMMUTABLE_VAT_FIELDS = ("ddv_rezim_sifra", "ddv_zahteva_polni_odbitek")
SOURCE_FIELD = "credit_line_source_v1"
PDF_FIELD = "correction_pdf_references_v1"


def line_fingerprint(line):
    values = {field.attname: getattr(line, field.attname) for field in line._meta.concrete_fields}
    return hashlib.sha256(json.dumps(
        values, cls=DjangoJSONEncoder, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def standalone_original(original):
    return original.document_type == Invoice.DocumentType.ST and original.lease_id is None


def _entries(record, field):
    return AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(record), object_id=record.pk, polje=field,
    )


def _read(record, field):
    try:
        return json.loads(_entries(record, field).get().nova_vrednost)
    except (AuditLogEntry.DoesNotExist, AuditLogEntry.MultipleObjectsReturned,
            ValueError, TypeError) as error:
        raise ValidationError("Zgodovinsko dokazilo korekcije manjka ali ni veljavno.") from error


def original_references(original):
    from .st_confirmation import preveri_st_pdf_referencne_zahteve

    lines = list(original.invoiceline_set.order_by("pk"))
    preveri_st_pdf_referencne_zahteve(original, lines)
    return _read(original, "st_pdf_references_v1")["lines"]


def _check_source(invoice, source):
    if not isinstance(source, InvoiceLine) or source.pk is None or source._state.adding:
        raise ValidationError("Zahtevana je shranjena izvirna postavka.")
    source = InvoiceLine.objects.get(pk=source.pk)
    original = invoice.povezan_racun
    if (source.invoice_id != original.pk or original.company_id != invoice.company_id
            or not original.stevilka or original.status == Invoice.Status.OSNUTEK
            or original.document_type not in ("NP", "ST") or original.povezan_racun_id):
        raise ValidationError("Izvirna postavka ne pripada izvirniku tega dobropisa.")
    return source


def _check_vat(line, source):
    if any(getattr(line, name) != getattr(source, name) for name in (*VAT_FIELDS, *IMMUTABLE_VAT_FIELDS)):
        raise ValidationError("Zgodovinskih podatkov DDV izvirne postavke ni dovoljeno spreminjati.")
    if standalone_original(source.invoice):
        required = original_references(source.invoice)[str(source.pk)]["required"]
        if required and (not line.referenca_listine.strip() or line.datum_reference is None):
            raise ValidationError("Zgodovinsko zahtevana referenca listine in datum sta obvezna.")


def prepare_credit_source(line, source):
    if source is None:
        if standalone_original(line.invoice.povezan_racun):
            raise ValidationError("Za dobropis samostojnega ST izberite izvirno postavko z dokazili DDV.")
        if line.ddv_rezim_sifra or line.ddv_zahteva_polni_odbitek is not None:
            raise ValidationError("Ročna postavka brez izvora ne sme izmišljati zgodovinskih dokazil DDV.")
        return None
    source = _check_source(line.invoice, source)
    for name in IMMUTABLE_VAT_FIELDS:
        setattr(line, name, getattr(source, name))
    _check_vat(line, source)
    return source


def record_credit_source(line, source):
    if source is not None:
        AuditLogEntry.objects.create(
            content_type=ContentType.objects.get_for_model(InvoiceLine), object_id=line.pk,
            polje=SOURCE_FIELD, nova_vrednost=json.dumps({
                "original": source.invoice_id, "source": source.pk, "fingerprint": line_fingerprint(source),
            }), razlog="Nespremenljiva povezava dobropisne postavke na zgodovinska dokazila izvirnika.",
        )


def credit_source(line):
    if not _entries(line, SOURCE_FIELD).exists():
        prepare_credit_source(line, None)
        return None
    evidence = _read(line, SOURCE_FIELD)
    try:
        source = InvoiceLine.objects.get(pk=evidence["source"])
        if (evidence["original"] != line.invoice.povezan_racun_id
                or evidence["fingerprint"] != line_fingerprint(source)):
            raise ValueError
        source = _check_source(line.invoice, source)
        _check_vat(line, source)
    except (KeyError, TypeError, ValueError, InvoiceLine.DoesNotExist) as error:
        raise ValidationError("Izvor zgodovinskih dokazil DDV ni veljaven.") from error
    return source


def validate_st_history(invoice, original, lines):
    """Use issued snapshots and immutable reference evidence, never live catalogs."""
    from .services import (
        _SNAPSHOT_POLJA_KOREKCIJE,
        _company_validation_issues,
        _tenant_validation_issues,
    )
    from .st_confirmation import _RECIPIENT

    number = re.fullmatch(r"ST-([0-9]{4})-([0-9]{4,})", original.stevilka or "")
    if (not standalone_original(original) or original.povezan_racun_id
            or original.status in (Invoice.Status.OSNUTEK, Invoice.Status.ARHIVIRAN_NI_IZDAN)
            or original.company_id != invoice.company_id or invoice.lease_id is not None
            or invoice.prejemnik_id is not None or invoice.oznaka_pogodbe or invoice.objekt_naziv
            or number is None or int(number[1]) != original.datum_izdaje.year or int(number[2]) < 1
            or original.eslog_konfiguracija_snapshot is not None
            or original.rendered_eslog_path or original.rendered_eslog_checksum
            or invoice.obdobje_od != original.obdobje_od or invoice.obdobje_do != original.obdobje_do
            or original.obdobje_od > original.obdobje_do
            or any(getattr(invoice, name) != getattr(original, name) for name in _SNAPSHOT_POLJA_KOREKCIJE)):
        raise ValidationError("Neveljaven izvirnik ali povezave korekcije samostojnega ST.")
    from companies.models import Company

    issuer = Company(**{name: getattr(original, f"izdajatelj_{name}") for name in (
        "naziv", "naslov", "davcna_stevilka", "maticna_stevilka", "drzava_koda", "trr",
    )})
    recipient = Tenant(**{name: getattr(original, f"prejemnik_{snap}") for snap, name in _RECIPIENT.items()})
    if (_company_validation_issues(issuer) or _tenant_validation_issues(recipient)
            or type(original.prejemnik_polni_odbitek) is not bool
            or type(original.prejemnik_identificiran_za_ddv) is not bool
            or type(original.izdajatelj_identificiran_za_ddv) is not bool):
        raise ValidationError("Zgodovinski posnetek partnerjev ST ni veljaven.")
    requirements = original_references(original)
    sources = list(original.invoiceline_set.order_by("pk"))
    if any(getattr(original, total) != sum((getattr(source, field) for source in sources), Decimal("0.00"))
           for field, total in (("osnova", "osnova_skupaj"), ("ddv_znesek", "ddv_skupaj"), ("skupaj", "skupaj_z_ddv"))):
        raise ValidationError("Zgodovinske vsote ST niso skladne s postavkami.")
    for source in sources:
        source.full_clean()
        if (source.lease_id or source.lease_item_id or source.adjustment_id or source.rent_indexation_event_id
                or source.ddv_stopnja < 0 or not source.enota_mere.strip()):
            raise ValidationError("Neveljavna zgodovinska postavka ST.")
        rate = source.ddv_stopnja if source.ddv_nacin_obracuna == "OBDAVCENO" else Decimal("0.00")
        if source.skupaj <= 0 or (source.osnova, source.ddv_znesek) != izracunaj_iz_bruto(source.skupaj, rate):
            raise ValidationError("Neveljavni zgodovinski zneski ST.")
    result = {}
    for line in lines:
        if invoice.document_type == "DOBROPIS":
            matches = [credit_source(line)]
        else:
            from .services import _kljuc_storno_vrstice, _storno_vrstica

            matches = [source for source in sources
                       if _kljuc_storno_vrstice(_storno_vrstica(source, invoice)) == _kljuc_storno_vrstice(line)]
        if not matches or any(source is None for source in matches):
            raise ValidationError("Manjka izvirna postavka z dokazili DDV.")
        required = any(requirements[str(source.pk)]["required"] for source in matches)
        preveri_upravicenost_prejemnika(
            sifra=line.ddv_rezim_sifra, zahteva_polni_odbitek=line.ddv_zahteva_polni_odbitek,
            tip=invoice.prejemnik_tip, identificiran_za_ddv=invoice.prejemnik_identificiran_za_ddv,
            polni_odbitek=invoice.prejemnik_polni_odbitek,
            najemnik=invoice.prejemnik_naziv, postavka=line.opis,
        )
        if required and (not line.referenca_listine.strip() or line.datum_reference is None):
            raise ValidationError("Zgodovinsko zahtevana referenca listine in datum sta obvezna.")
        result[str(line.pk)] = {"required": required, "regime": line.ddv_rezim_sifra,
                                "fingerprint": line_fingerprint(line)}
    return result


def record_st_correction_pdf(invoice, references):
    AuditLogEntry.objects.create(
        content_type=ContentType.objects.get_for_model(Invoice), object_id=invoice.pk,
        polje=PDF_FIELD, nova_vrednost=json.dumps({
            "number": invoice.stevilka, "template": invoice.pdf_template_version,
            "original": invoice.povezan_racun_id, "lines": references,
        }), razlog="Nespremenljiva dokazila DDV in referenc korekcije samostojnega ST ob izdaji.",
    )


def validate_st_correction_pdf(invoice, lines):
    evidence = _read(invoice, PDF_FIELD)
    expected = {"number": invoice.stevilka, "template": invoice.pdf_template_version,
                "original": invoice.povezan_racun_id,
                "lines": validate_st_history(invoice, invoice.povezan_racun, lines)}
    if evidence != expected:
        raise ValidationError("Nespremenljivo dokazilo PDF korekcije ST ni skladno.")
