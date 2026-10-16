"""Standalone ST domain confirmation; rendering is a separate explicit operation.

No UI calls these services. Review evidence is an immutable audit entry, bound to
the saved draft and its material sources. Every repeated confirmation is rejected.
"""

import json
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import connection, transaction

from catalogs.models import VatRegime
from companies.models import NumberSequence
from companies.services import (
    _kanonicni_fingerprint,
    dodeli_naslednjo_stevilko,
    izberi_veljavnega_podpisnika,
    shrani_nespremenljiv_asset,
)
from contracts.models import Tenant
from contracts.vat_eligibility import preveri_upravicenost_prejemnika
from core.models import AuditLogEntry
from core.pravila import izracunaj_iz_bruto

from .models import Invoice

_ISSUER = {
    "naziv": "naziv", "naslov": "naslov", "drzava_koda": "drzava_koda",
    "identificiran_za_ddv": "ddv_zavezanec", "davcna_stevilka": "davcna_stevilka",
    "maticna_stevilka": "maticna_stevilka", "trr": "trr",
}
_RECIPIENT = {
    "naziv": "naziv", "naslov": "naslov", "tip": "tip", "drzava_koda": "drzava_koda",
    "identificiran_za_ddv": "identificiran_za_ddv",
    "polni_odbitek": "ddv_zavezanec_s_polnim_odbitkom",
    "davcna_stevilka": "davcna_stevilka", "maticna_stevilka": "maticna_stevilka",
    "ddv_identifikacijska_stevilka": "ddv_identifikacijska_stevilka",
}
_VAT = {
    "ddv_stopnja": "stopnja", "ddv_nacin_obracuna": "nacin_obracuna",
    "ddv_zahteva_polni_odbitek": "zahteva_polni_odbitek", "ddv_klavzula": "klavzula",
    "ddv_rekap_skupina": "rekapitulacijska_skupina", "ddv_eslog_koda": "eslog_koda",
}
_REVIEW_FIELD = "st_individual_review_v1"
_VAT_SOURCE_FIELD = "st_vat_source_v1"
_PDF_REFERENCES_FIELD = "st_pdf_references_v1"


def _values(record):
    return {field.attname: getattr(record, field.attname) for field in record._meta.concrete_fields}


def _record_st_vat_source(invoice, regime):
    """Bind unsnapshotted VAT conditions (including reference requirements) at creation."""
    AuditLogEntry.objects.create(
        content_type=ContentType.objects.get_for_model(Invoice), object_id=invoice.pk,
        polje=_VAT_SOURCE_FIELD, nova_vrednost=_kanonicni_fingerprint(_values(regime)),
        razlog="Nespremenljivo dokazilo DDV vira ob pripravi ST osnutka.",
    )


def _load(osnutek):
    if not isinstance(osnutek, Invoice) or osnutek.pk is None or osnutek._state.adding:
        raise ValidationError("ST zahteva shranjen osnutek.")
    invoice = Invoice.objects.select_related("company", "prejemnik").get(pk=osnutek.pk)
    if (invoice.document_type != Invoice.DocumentType.ST
            or invoice.status != Invoice.Status.OSNUTEK or invoice.stevilka is not None
            or invoice.lease_id is not None or invoice.povezan_racun_id is not None):
        raise ValidationError("ST zahteva samostojen neoštevilčen osnutek.")
    return invoice, list(invoice.invoiceline_set.order_by("pk"))


def _validate(invoice, lines):
    # Import shared validators lazily: this module is also exposed by services.py.
    from .services import (
        _company_validation_issues,
        _preveri_ddv_klavzule,
        _preveri_eslog_kodo,
        _preveri_mesanje_eslog_o,
        _tenant_validation_issues,
    )

    invoice.full_clean()
    if (any(type(value) is not date for value in (
            invoice.datum_izdaje, invoice.obdobje_od, invoice.obdobje_do))
            or invoice.obdobje_od > invoice.obdobje_do):
        raise ValidationError("Neveljaven datum izdaje ali obdobje ST.")
    if (invoice.oznaka_pogodbe or invoice.objekt_naziv or invoice.razlog
            or invoice.razlog_arhiviranja or invoice.rendered_pdf_path or invoice.checksum
            or invoice.rendered_eslog_path or invoice.rendered_eslog_checksum
            or invoice.eslog_konfiguracija_snapshot is not None
            or invoice.dostavni_kontakti_snapshot):
        raise ValidationError("ST osnutek vsebuje nepodprte povezave ali izdane dokumente.")
    if not invoice.company.aktivno or _company_validation_issues(invoice.company):
        raise ValidationError("Neveljaven izdajatelj ST.")
    if any(getattr(invoice, f"izdajatelj_{snap}") != getattr(invoice.company, source)
           for snap, source in _ISSUER.items()):
        raise ValidationError("Posnetek izdajatelja ST se ne ujema; potreben je nov pregled.")
    recipient = Tenant(**{
        source: getattr(invoice, f"prejemnik_{snap}") for snap, source in _RECIPIENT.items()
    })
    if (type(recipient.identificiran_za_ddv) is not bool
            or type(recipient.ddv_zavezanec_s_polnim_odbitkom) is not bool
            or _tenant_validation_issues(recipient)):
        raise ValidationError("Neveljaven posnetek prejemnika ST.")
    if invoice.prejemnik_id and (
        not invoice.prejemnik.aktiven or any(
            getattr(invoice, f"prejemnik_{snap}") != getattr(invoice.prejemnik, source)
            for snap, source in _RECIPIENT.items()
        )
    ):
        raise ValidationError("Posnetek prejemnika ST se ne ujema; potreben je nov pregled.")
    signatory = izberi_veljavnega_podpisnika(invoice.company, invoice.datum_izdaje)
    signatory.full_clean(exclude=["podpis_slika"])
    if (invoice.izdajatelj_podpisnik_ime != signatory.ime_priimek
            or invoice.izdajatelj_podpisnik_funkcija != signatory.funkcija
            or invoice.izdajatelj_podpisnik_slika_pot != signatory.podpis_slika.name
            or invoice.izdajatelj_logotip_slika_pot or invoice.pdf_template_version):
        raise ValidationError("Posnetek podpisnika ali dokumenta ST se ne ujema.")
    if not lines:
        raise ValidationError("ST osnutek nima vrstic.")
    regimes = []
    for line in lines:
        line.full_clean()
        if (line.lease_id or line.lease_item_id or line.adjustment_id
                or line.rent_indexation_event_id or line.adjustment_snapshot
                or line.je_sorazmerno or line.izvirno_izracunana_vrednost is not None
                or line.razlog_rocnega_popravka or not line.opis.strip()
                or not line.enota_mere.strip()):
            raise ValidationError("Neveljavna samostojna ST vrstica.")
        regime = VatRegime.objects.filter(sifra=line.ddv_rezim_sifra).first()
        if regime is None:
            raise ValidationError("DDV režim ST ne obstaja več.")
        if not AuditLogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(Invoice), object_id=invoice.pk,
            polje=_VAT_SOURCE_FIELD, nova_vrednost=_kanonicni_fingerprint(_values(regime)),
        ).exists():
            raise ValidationError("Dokazilo DDV vira ST manjka ali je zastarelo; pripravite nov osnutek.")
        regime.full_clean()
        if (not regime.aktiven or regime.stopnja < 0
                or not regime.rekapitulacijska_skupina.strip()
                or not regime.velja_od <= invoice.datum_izdaje
                or (regime.velja_do is not None and invoice.datum_izdaje > regime.velja_do)
                or any(getattr(line, snap) != getattr(regime, source)
                       for snap, source in _VAT.items())):
            raise ValidationError("Posnetek DDV ST se ne ujema; potreben je nov pregled.")
        if regime.zahteva_referenco and (
            not line.referenca_listine.strip() or type(line.datum_reference) is not date
        ):
            raise ValidationError("Referenca listine in datum reference sta obvezna za ST.")
        preveri_upravicenost_prejemnika(
            sifra=line.ddv_rezim_sifra, zahteva_polni_odbitek=line.ddv_zahteva_polni_odbitek,
            tip=invoice.prejemnik_tip, identificiran_za_ddv=invoice.prejemnik_identificiran_za_ddv,
            polni_odbitek=invoice.prejemnik_polni_odbitek,
            najemnik=invoice.prejemnik_naziv, postavka=line.opis,
        )
        rate = line.ddv_stopnja if line.ddv_nacin_obracuna == "OBDAVCENO" else Decimal("0.00")
        if (line.skupaj <= 0 or line.skupaj != line.osnova + line.ddv_znesek
                or (line.osnova, line.ddv_znesek) != izracunaj_iz_bruto(line.skupaj, rate)):
            raise ValidationError("Neveljavni denarni zneski ST vrstice.")
        _preveri_eslog_kodo(line.ddv_nacin_obracuna, line.ddv_eslog_koda, "ST posnetek")
        regimes.append(_values(regime))
    _preveri_ddv_klavzule(lines)
    _preveri_mesanje_eslog_o(lines)
    totals = {
        "osnova_skupaj": sum((line.osnova for line in lines), Decimal("0.00")),
        "ddv_skupaj": sum((line.ddv_znesek for line in lines), Decimal("0.00")),
        "skupaj_z_ddv": sum((line.skupaj for line in lines), Decimal("0.00")),
    }
    for field, value in totals.items():
        Invoice._meta.get_field(field).clean(value, invoice)
        if getattr(invoice, field) not in (None, value):
            raise ValidationError("Shranjene vsote ST se ne ujemajo z vrsticami.")
    try:
        deadline = invoice.datum_izdaje + timedelta(days=invoice.company.rok_placila_dni_privzeto)
    except OverflowError as error:
        raise ValidationError("Neveljaven rok plačila ST.") from error
    if deadline < invoice.datum_izdaje or invoice.rok_placila not in (None, deadline):
        raise ValidationError("Neveljaven rok plačila ST.")
    return signatory, regimes, totals | {"rok_placila": deadline}


def _state(osnutek):
    invoice, lines = _load(osnutek)
    signatory, regimes, final = _validate(invoice, lines)
    fingerprint = _kanonicni_fingerprint({
        "version": 1, "invoice": _values(invoice), "lines": [_values(line) for line in lines],
        "issuer": {source: getattr(invoice.company, source) for source in _ISSUER.values()},
        "payment_days": invoice.company.rok_placila_dni_privzeto,
        "logo": [invoice.company.logotip.name, invoice.company.logotip_checksum],
        "signatory": _values(signatory) | {"podpis_slika": signatory.podpis_slika.name},
        "regimes": regimes, "template": settings.PDF_TEMPLATE_VERSION,
        "st_template": settings.ST_PDF_TEMPLATE_VERSION,
    })
    references = {
        str(line.pk): {"regime": line.ddv_rezim_sifra, "required": regime["zahteva_referenco"]}
        for line, regime in zip(lines, regimes, strict=True)
    }
    return invoice, fingerprint, signatory, final, references


def fingerprint_osnutka_st(osnutek: Invoice) -> str:
    """Return the validated saved state that the individual reviewer must inspect."""
    return _state(osnutek)[1]


def _outside_transaction():
    if connection.in_atomic_block:
        raise ValidationError("ST pregled in potrditev morata začeti zunaj transakcije.")


def _assets(invoice, signatory):
    evidence = {}
    for path, checksum in (
        (invoice.company.logotip.name, invoice.company.logotip_checksum),
        (signatory.podpis_slika.name, signatory.podpis_checksum),
    ):
        immutable = shrani_nespremenljiv_asset(path)
        if not checksum or immutable != f"nespremenljivi-asseti/{checksum}":
            raise ValidationError("Vsebina logotipa ali podpisa se ne ujema s preverjenim vzorcem.")
        evidence[path] = immutable
    return evidence


def preglej_osnutek_st(
    osnutek: Invoice, *, expected_fingerprint: str, individual_review: bool,
) -> AuditLogEntry:
    """Record explicit review of an exact saved state; reading T8-B records nothing."""
    _outside_transaction()
    if individual_review is not True:
        raise ValidationError("Obvezen je izrecen posamični pregled ST.")
    invoice, fingerprint, signatory, _, _ = _state(osnutek)
    if fingerprint != expected_fingerprint:
        raise ValidationError("Pregled ST je zastarel.")
    assets = _assets(invoice, signatory)
    with transaction.atomic():
        if _state(osnutek)[1] != fingerprint:
            raise ValidationError("Pregled ST je zastarel.")
        return AuditLogEntry.objects.create(
            content_type=ContentType.objects.get_for_model(Invoice), object_id=invoice.pk,
            polje=_REVIEW_FIELD, nova_vrednost=json.dumps({"fingerprint": fingerprint, "assets": assets}),
            razlog="Izrecen posamični pregled shranjenega ST osnutka.",
        )


def _review(osnutek, pregled):
    invoice, fingerprint, signatory, final, references = _state(osnutek)
    entry = AuditLogEntry.objects.filter(
        pk=getattr(pregled, "pk", None),
        content_type=ContentType.objects.get_for_model(Invoice),
        object_id=invoice.pk, polje=_REVIEW_FIELD,
    ).first()
    try:
        evidence = json.loads(entry.nova_vrednost) if entry else {}
        if evidence.get("fingerprint") != fingerprint or not isinstance(evidence.get("assets"), dict):
            raise ValueError
    except (ValueError, TypeError, AttributeError) as error:
        raise ValidationError("Posamični pregled ST manjka ali je zastarel.") from error
    return invoice, signatory, final, evidence["assets"], references


def preveri_st_pdf_referencne_zahteve(invoice: Invoice, lines) -> None:
    """Read confirmation evidence only; never recover requirements from live VAT data."""
    try:
        entry = AuditLogEntry.objects.get(
            content_type=ContentType.objects.get_for_model(Invoice), object_id=invoice.pk,
            polje=_PDF_REFERENCES_FIELD,
        )
        evidence = json.loads(entry.nova_vrednost)
        if (evidence["number"] != invoice.stevilka
                or evidence["template"] != invoice.pdf_template_version
                or not isinstance(evidence["lines"], dict)
                or set(evidence["lines"]) != {str(line.pk) for line in lines}):
            raise ValueError
        for line in lines:
            requirement = evidence["lines"][str(line.pk)]
            if (requirement["regime"] != line.ddv_rezim_sifra
                    or type(requirement["required"]) is not bool
                    or (requirement["required"] and (
                        not line.referenca_listine.strip() or line.datum_reference is None))):
                raise ValueError
    except (AuditLogEntry.DoesNotExist, AuditLogEntry.MultipleObjectsReturned,
            KeyError, TypeError, ValueError, AttributeError) as error:
        raise ValidationError("Nespremenljivo dokazilo DDV referenc ST manjka ali ni skladno.") from error


def potrdi_osnutek_st(osnutek: Invoice, *, pregled: AuditLogEntry | None = None) -> Invoice:
    """Confirm once, without PDF/eSLOG, sending or archiving. Retries always reject.

    Files are verified/frozen before the short IMMEDIATE transaction. The locked
    reread revalidates the review, snapshots and every production readiness gate.
    """
    _outside_transaction()
    invoice, signatory, _, assets, _ = _review(osnutek, pregled)
    if _assets(invoice, signatory) != assets:
        raise ValidationError("Asseti pregleda ST so zastareli.")
    with transaction.atomic():
        invoice, signatory, final, reviewed_assets, references = _review(osnutek, pregled)
        if reviewed_assets != assets:
            raise ValidationError("Pregled ST je zastarel.")
        invoice.izdajatelj_logotip_slika_pot = assets[invoice.company.logotip.name]
        invoice.izdajatelj_podpisnik_slika_pot = assets[signatory.podpis_slika.name]
        invoice.pdf_template_version = settings.ST_PDF_TEMPLATE_VERSION
        for field, value in final.items():
            setattr(invoice, field, value)
        invoice.full_clean()
        number = dodeli_naslednjo_stevilko(
            invoice.company, NumberSequence.Tip.ST, invoice.datum_izdaje, asset_evidence=assets,
        )
        invoice.stevilka = f"ST-{invoice.datum_izdaje.year}-{number:04d}"
        invoice.status = Invoice.Status.IZBRAN_ZA_POSILJANJE
        invoice.save(update_fields=[
            *final, "stevilka", "status", "izdajatelj_logotip_slika_pot",
            "izdajatelj_podpisnik_slika_pot", "pdf_template_version",
        ])
        # _review verified these flags against the immutable VAT source hash and
        # individual review in this transaction. Publish evidence with the number;
        # a failure rolls back both. Issued financial snapshots remain untouched.
        AuditLogEntry.objects.create(
            content_type=ContentType.objects.get_for_model(Invoice), object_id=invoice.pk,
            polje=_PDF_REFERENCES_FIELD, nova_vrednost=json.dumps({
                "number": invoice.stevilka, "template": invoice.pdf_template_version,
                "lines": references,
            }),
            razlog="Nespremenljive zahteve DDV referenc ob potrditvi ST.",
        )
        return invoice
