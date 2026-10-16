"""Standalone ST confirmation uses synthetic setup, files and isolated databases."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction

from archive.models import ArchivedFile, BackupSnapshot
from catalogs.models import VatRegime
from companies.models import (
    Company,
    InitialSetupCompletion,
    NumberingVerification,
    NumberSequence,
)
from companies.readiness import verify_numbering
from core.models import AuditLogEntry
from delivery.models import DeliveryAttempt
from invoicing import st_confirmation
from invoicing.models import Invoice, InvoiceLine
from invoicing.services import (
    fingerprint_osnutka_st,
    potrdi_korekcijski_osnutek,
    potrdi_osnutek_np,
    potrdi_osnutek_st,
    preglej_osnutek_st,
    pripravi_osnutek_st,
)
from tests.business_fixtures import save_business_record
from tests.financial_fixtures import save_financial_fixture, update_financial_fixture
from tests.setup_helpers import prepare_initial_setup
from tests.ui import test_company_confirmations

setup_data = test_company_confirmations.confirmation_ui_data

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def ready(setup_data, settings):
    # Explicit settings restoration prevents helper defaults leaking into other tests.
    settings.MARJAN_SMTP_HOST = "smtp.example.test"
    settings.MARJAN_SMTP_PORT = 587
    settings.MARJAN_SMTP_SECURITY = "STARTTLS"
    settings.MARJAN_SMTP_TIMEOUT_SECONDS = 10
    settings.MARJAN_KEYRING_SERVICE = "test-marjanapp"
    prepare_initial_setup(setup_data.company, setup_data.lease)
    setup_data.company.refresh_from_db()
    return setup_data


def draft(data, **changes):
    return pripravi_osnutek_st(**({
        "company": data.company, "prejemnik": data.lease.tenant,
        "vat_regime": data.regime, "datum_izdaje": date(2026, 10, 1),
        "obdobje_od": date(2026, 9, 1), "obdobje_do": date(2026, 9, 30),
        "bruto": Decimal("122.00"), "opis": "Svetovanje", "enota_mere": "ura",
    } | changes))


def review(invoice, **changes):
    return preglej_osnutek_st(invoice, **({
        "expected_fingerprint": fingerprint_osnutka_st(invoice), "individual_review": True,
    } | changes))


def counters():
    return list(NumberSequence.objects.order_by("pk").values())


def assert_unchanged(invoice, before, numbers):
    assert Invoice.objects.filter(pk=invoice.pk).values().get() == before
    assert counters() == numbers


def test_valid_confirmation_preserves_snapshots_and_has_no_operational_effects(ready):
    invoice = draft(ready)
    proof = review(invoice)
    before = Invoice.objects.filter(pk=invoice.pk).values().get()
    line_before = list(invoice.invoiceline_set.values())
    with ExitStack() as stack:
        # Setup sample rendering finishes before these guards are installed.
        for name in (
            "rendering.services._generiraj_pdf_bytes", "smtplib.SMTP", "smtplib.SMTP_SSL",
            "keyring.get_password", "invoicing.services._razresi_eslog_snapshot",
            "invoicing.services.razresi_eslog_konfiguracijo",
            "archive.configuration.validate_archive_locations",
        ):
            stack.enter_context(patch(name, side_effect=AssertionError(name)))
        result = potrdi_osnutek_st(invoice, pregled=proof)
    assert result.stevilka == "ST-2026-0001"
    assert result.status == Invoice.Status.IZBRAN_ZA_POSILJANJE
    assert result.rok_placila == date(2026, 10, 16)
    assert (result.osnova_skupaj, result.ddv_skupaj, result.skupaj_z_ddv) == (
        Decimal("100.00"), Decimal("22.00"), Decimal("122.00"),
    )
    changed = {
        "stevilka", "status", "rok_placila", "osnova_skupaj", "ddv_skupaj", "skupaj_z_ddv",
        "izdajatelj_logotip_slika_pot", "izdajatelj_podpisnik_slika_pot", "pdf_template_version",
    }
    after = Invoice.objects.filter(pk=invoice.pk).values().get()
    assert {key: value for key, value in before.items() if key not in changed} == {
        key: value for key, value in after.items() if key not in changed
    }
    assert list(invoice.invoiceline_set.values()) == line_before
    assert result.izdajatelj_podpisnik_slika_pot.startswith("nespremenljivi-asseti/")
    assert result.izdajatelj_logotip_slika_pot.startswith("nespremenljivi-asseti/")
    assert result.eslog_konfiguracija_snapshot is None
    assert not result.rendered_pdf_path and not result.rendered_eslog_path
    assert not DeliveryAttempt.objects.exists()
    assert not ArchivedFile.objects.exists() and not BackupSnapshot.objects.exists()
    assert NumberSequence.objects.get(company=ready.company, tip="NP").zadnja_stevilka == 0


def test_review_must_be_explicit_and_for_exact_saved_state(ready, client):
    invoice = draft(ready)
    fingerprint = fingerprint_osnutka_st(invoice)
    with pytest.raises(ValidationError, match="posamični"):
        review(invoice, individual_review=False)
    with pytest.raises(ValidationError, match="pregled"):
        potrdi_osnutek_st(invoice)
    url = f"/storitveni-racuni/{invoice.company_id}/{invoice.pk}/"
    count = AuditLogEntry.objects.filter(polje="st_individual_review_v1").count()
    assert client.get(url).status_code == 200
    assert client.post(url).status_code == 405
    assert AuditLogEntry.objects.filter(polje="st_individual_review_v1").count() == count
    InvoiceLine.objects.filter(invoice=invoice).update(opis="Spremenjeno")
    with pytest.raises(ValidationError, match="zastarel"):
        review(invoice, expected_fingerprint=fingerprint)
    proof = review(invoice)
    assert proof.object_id == invoice.pk
    with pytest.raises(ValidationError, match="nespremenljiva"):
        proof.nova_vrednost = "forged"
        proof.save()


@pytest.mark.parametrize("change", ["invoice", "line", "add_line", "recipient", "issuer", "vat", "signatory", "default", "template"])
def test_stale_review_and_source_changes_fail_without_refresh(ready, settings, change):
    invoice = draft(ready)
    proof = review(invoice)
    if change == "invoice":
        Invoice.objects.filter(pk=invoice.pk).update(obdobje_od=date(2026, 9, 2))
    elif change == "line":
        InvoiceLine.objects.filter(invoice=invoice).update(opis="Drugo")
    elif change == "add_line":
        line = invoice.invoiceline_set.get()
        line.pk = None
        line.save()
    elif change == "recipient":
        ready.lease.tenant.naziv = "Spremenjen prejemnik"
        save_business_record(ready.lease.tenant)
    elif change == "issuer":
        ready.company.naziv = "Spremenjen izdajatelj"
        save_financial_fixture(ready.company)
    elif change == "vat":
        ready.regime.stopnja = Decimal("9.50")
        save_financial_fixture(ready.regime)
    elif change == "signatory":
        signatory = ready.company.signatory_set.get()
        signatory.ime_priimek = "Drug podpisnik"
        save_business_record(signatory)
    elif change == "default":
        ready.company.rok_placila_dni_privzeto = 30
        save_financial_fixture(ready.company)
    else:
        settings.PDF_TEMPLATE_VERSION = "changed"
    before, numbers = Invoice.objects.filter(pk=invoice.pk).values().get(), counters()
    with pytest.raises((ValidationError, ValueError)):
        potrdi_osnutek_st(invoice, pregled=proof)
    assert_unchanged(invoice, before, numbers)


@pytest.mark.parametrize("blocker", ["setup", "audit", "numbering", "numbering_audit", "sample", "vat", "inactive", "smtp", "archive"])
def test_all_production_readiness_gates_remain_required(ready, settings, blocker):
    invoice = draft(ready)
    proof = review(invoice)
    if blocker == "setup":
        InitialSetupCompletion.objects.all().delete()
    elif blocker == "audit":
        InitialSetupCompletion.objects.update(evidence={})
    elif blocker == "numbering":
        NumberingVerification.objects.filter(company=ready.company).delete()
    elif blocker == "numbering_audit":
        NumberingVerification.objects.filter(company=ready.company).update(np_last=1)
    elif blocker == "sample":
        ready.company.sample_confirmations.all().delete()
    elif blocker == "vat":
        ready.company.vat_settings_confirmations.all().delete()
    elif blocker == "inactive":
        update_financial_fixture(Company.objects.filter(pk=ready.company.pk), aktivno=False)
    elif blocker == "smtp":
        settings.MARJAN_SMTP_HOST = ""
    else:
        from archive.models import ArchiveConfiguration

        ArchiveConfiguration.objects.update(verification_evidence={})
    before, numbers = Invoice.objects.filter(pk=invoice.pk).values().get(), counters()
    with pytest.raises((ValidationError, ValueError)):
        potrdi_osnutek_st(invoice, pregled=proof)
    assert_unchanged(invoice, before, numbers)


def test_numbering_is_per_year_per_company_and_does_not_change_np(ready):
    other = Company.objects.exclude(pk=ready.company.pk).first()
    update_financial_fixture(Company.objects.filter(pk=other.pk), aktivno=True)
    before_np = list(NumberSequence.objects.filter(tip="NP").values())
    numbers = []
    for issuer, year in ((ready.company, 2026), (ready.company, 2026),
                         (ready.company, 2027), (other, 2026)):
        invoice = draft(ready, company=issuer, datum_izdaje=date(year, 10, 1))
        numbers.append(potrdi_osnutek_st(invoice, pregled=review(invoice)).stevilka)
    assert numbers == ["ST-2026-0001", "ST-2026-0002", "ST-2027-0001", "ST-2026-0001"]
    assert list(NumberSequence.objects.filter(tip="NP").values()) == before_np


def test_repeated_confirmation_rejects_without_second_number(ready):
    invoice = draft(ready)
    proof = review(invoice)
    result = potrdi_osnutek_st(invoice, pregled=proof)
    before, numbers = Invoice.objects.filter(pk=invoice.pk).values().get(), counters()
    for argument in (invoice, result):
        with pytest.raises(ValidationError, match="neoštevilčen"):
            potrdi_osnutek_st(argument, pregled=proof)
    assert_unchanged(invoice, before, numbers)


@pytest.mark.parametrize("failure", ["after_validation", "after_allocation", "save"])
def test_precommit_failures_roll_back_invoice_and_numbering(ready, failure):
    invoice = draft(ready, datum_izdaje=date(2027, 1, 1))
    proof = review(invoice)
    before, numbers = Invoice.objects.filter(pk=invoice.pk).values().get(), counters()
    allocate = st_confirmation.dodeli_naslednjo_stevilko

    def fail(*args, **kwargs):
        if failure == "after_allocation":
            allocate(*args, **kwargs)
        raise RuntimeError("Injected failure")

    target = "invoicing.st_confirmation.dodeli_naslednjo_stevilko" if failure != "save" else "invoicing.models.Invoice.save"
    with patch(target, side_effect=fail), pytest.raises(RuntimeError, match="Injected"):
        potrdi_osnutek_st(invoice, pregled=proof)
    assert_unchanged(invoice, before, numbers)


@pytest.mark.parametrize("field,value", [
    ("document_type", "NP"), ("document_type", "DOBROPIS"), ("stevilka", ""),
    ("status", "POSLAN"), ("prejemnik_naziv", "Drug"), ("izdajatelj_naziv", "Drug"),
    ("obdobje_od", date(2027, 1, 1)), ("skupaj_z_ddv", Decimal("1.00")),
    ("rok_placila", date(2026, 9, 1)), ("rendered_pdf_path", "existing.pdf"),
    ("eslog_konfiguracija_snapshot", {}),
])
def test_invalid_persisted_invoice_is_rejected(ready, field, value):
    invoice = draft(ready)
    proof = review(invoice)
    Invoice.objects.filter(pk=invoice.pk).update(**{field: value})
    before, numbers = Invoice.objects.filter(pk=invoice.pk).values().get(), counters()
    with pytest.raises((ValidationError, ValueError)):
        potrdi_osnutek_st(invoice, pregled=proof)
    assert_unchanged(invoice, before, numbers)


@pytest.mark.parametrize("field,value", [
    ("osnova", Decimal("99.99")), ("ddv_znesek", Decimal("21.99")),
    ("skupaj", Decimal("0.00")), ("skupaj", Decimal("-1.00")),
    ("ddv_stopnja", Decimal("9.50")), ("ddv_nacin_obracuna", "NI_PREDMET"),
    ("ddv_rezim_sifra", "missing"), ("ddv_zahteva_polni_odbitek", None),
    ("opis", " "), ("enota_mere", " "),
])
def test_invalid_persisted_line_is_rejected_even_before_review(ready, field, value):
    invoice = draft(ready)
    InvoiceLine.objects.filter(invoice=invoice).update(**{field: value})
    with pytest.raises((ValidationError, ValueError)):
        review(invoice)


@pytest.mark.parametrize("field,value", [("referenca_listine", ""), ("datum_reference", None)])
def test_required_reference_and_date_are_revalidated(ready, field, value):
    ready.regime.zahteva_referenco = True
    save_financial_fixture(ready.regime)
    invoice = draft(ready, referenca_listine="IZJAVA-1", datum_reference=date(2026, 9, 1))
    InvoiceLine.objects.filter(invoice=invoice).update(**{field: value})
    with pytest.raises(ValidationError, match="Referenca"):
        review(invoice)


@pytest.mark.parametrize("gross", ["0.01", "0.03", "1.00", "10.01"])
def test_exact_decimal_totals_and_company_deadline(ready, gross):
    invoice = draft(ready, bruto=Decimal(gross))
    result = potrdi_osnutek_st(invoice, pregled=review(invoice))
    assert result.osnova_skupaj + result.ddv_skupaj == result.skupaj_z_ddv == Decimal(gross)
    assert result.rok_placila == result.datum_izdaje + timedelta(days=ready.company.rok_placila_dni_privzeto)


@pytest.mark.parametrize("asset", ["logo", "signature", "immutable", "missing"])
def test_changed_or_missing_document_assets_block_confirmation(ready, settings, asset):
    invoice = draft(ready)
    proof = review(invoice)
    signatory = ready.company.signatory_set.get()
    relative = ready.company.logotip.name if asset == "logo" else signatory.podpis_slika.name
    if asset == "immutable":
        relative = f"nespremenljivi-asseti/{signatory.podpis_checksum}"
    path = Path(settings.MEDIA_ROOT) / relative
    if asset == "missing":
        path.unlink()
    else:
        path.write_bytes(b"Changed bytes")
    before, numbers = Invoice.objects.filter(pk=invoice.pk).values().get(), counters()
    with pytest.raises((ValidationError, ValueError)):
        potrdi_osnutek_st(invoice, pregled=proof)
    assert_unchanged(invoice, before, numbers)


@pytest.mark.parametrize("link", ["lease", "correction", "issuer", "recipient", "line_lease", "line_item"])
def test_wrong_persisted_relationships_fail_before_review(ready, link):
    invoice = draft(ready)
    if link == "lease":
        Invoice.objects.filter(pk=invoice.pk).update(lease=ready.lease)
    elif link == "correction":
        Invoice.objects.filter(pk=invoice.pk).update(povezan_racun=draft(ready))
    elif link == "issuer":
        Invoice.objects.filter(pk=invoice.pk).update(company=Company.objects.exclude(pk=ready.company.pk).first())
    elif link == "recipient":
        from contracts.models import Tenant

        other = Tenant.objects.create(
            tip="FIZICNA_OSEBA", naziv="Drug prejemnik", naslov="C 1", drzava_koda="SI",
            identificiran_za_ddv=False, ddv_zavezanec_s_polnim_odbitkom=False,
        )
        Invoice.objects.filter(pk=invoice.pk).update(prejemnik=other)
    else:
        InvoiceLine.objects.filter(invoice=invoice).update(**{
            "lease" if link == "line_lease" else "lease_item": ready.lease if link == "line_lease" else ready.item,
        })
    with pytest.raises((ValidationError, ValueError)):
        review(invoice)


def test_snapshot_only_recipient_and_multiple_lines(ready):
    invoice = draft(ready, prejemnik=None, prejemnik_podatki={
        "tip": "FIZICNA_OSEBA", "naziv": "Nov prejemnik", "naslov": "C 1",
        "drzava_koda": "SI", "identificiran_za_ddv": False,
        "ddv_zavezanec_s_polnim_odbitkom": False,
    })
    line = invoice.invoiceline_set.get()
    line.pk = None
    line.save()
    result = potrdi_osnutek_st(invoice, pregled=review(invoice))
    assert result.prejemnik_id is None
    assert (result.osnova_skupaj, result.ddv_skupaj, result.skupaj_z_ddv) == (
        Decimal("200.00"), Decimal("44.00"), Decimal("244.00"),
    )


def test_verified_seed_and_custom_company_payment_deadline(ready):
    verify_numbering(ready.company, np_last=0, st_last=7, st_year=2026, explicit=True)
    ready.company.rok_placila_dni_privzeto = 30
    save_financial_fixture(ready.company)
    invoice = draft(ready)
    result = potrdi_osnutek_st(invoice, pregled=review(invoice))
    assert result.stevilka == "ST-2026-0008"
    assert result.rok_placila == date(2026, 10, 31)


@pytest.mark.parametrize("mode,code", [
    ("OPROSCENO", "E"), ("NI_PREDMET", "O"), ("OBRNJENA_DAVCNA", "AE"),
])
def test_non_taxable_confirmation_preserves_nominal_rate(ready, mode, code):
    regime = VatRegime.objects.create(
        sifra=mode, naziv=mode, nacin_obracuna=mode, stopnja=Decimal("22.00"),
        klavzula="Davčna klavzula", rekapitulacijska_skupina=mode,
        zahteva_polni_odbitek=False, eslog_koda=code, velja_od=date(2020, 1, 1),
    )
    invoice = draft(ready, vat_regime=regime)
    result = potrdi_osnutek_st(invoice, pregled=review(invoice))
    line = result.invoiceline_set.get()
    assert result.osnova_skupaj == result.skupaj_z_ddv == Decimal("122.00")
    assert result.ddv_skupaj == Decimal("0.00")
    assert line.ddv_stopnja == Decimal("22.00")
    assert line.ddv_nacin_obracuna == mode and line.ddv_eslog_koda == code


def test_snapshot_recipient_eligibility_is_revalidated(ready):
    regime = VatRegime.objects.create(
        sifra="ST_ELIGIBLE", naziv="Full deduction", nacin_obracuna="OBDAVCENO",
        stopnja=Decimal("22.00"), rekapitulacijska_skupina="OBD_22",
        zahteva_polni_odbitek=True, eslog_koda="S", velja_od=date(2020, 1, 1),
    )
    invoice = draft(ready, vat_regime=regime, prejemnik=None, prejemnik_podatki={
        "tip": "PRAVNA_OSEBA", "naziv": "Nov prejemnik", "naslov": "C 1",
        "drzava_koda": "SI", "davcna_stevilka": "15012557",
        "maticna_stevilka": "1234567000", "identificiran_za_ddv": True,
        "ddv_zavezanec_s_polnim_odbitkom": True,
    })
    Invoice.objects.filter(pk=invoice.pk).update(prejemnik_polni_odbitek=False)
    with pytest.raises(ValidationError, match="polno pravico"):
        review(invoice)


def test_historical_year_requires_audited_numbering_verification(ready):
    invoice = draft(ready, datum_izdaje=date(2025, 12, 31))
    proof = review(invoice)
    numbers = counters()
    with pytest.raises(ValidationError, match="zgodovinsko leto"):
        potrdi_osnutek_st(invoice, pregled=proof)
    assert counters() == numbers
    verify_numbering(ready.company, np_last=0, st_last=9, st_year=2025, explicit=True)
    assert potrdi_osnutek_st(invoice, pregled=proof).stevilka == "ST-2025-0010"


@pytest.mark.parametrize("field,value", [
    ("zahteva_referenco", True), ("velja_od", date(2021, 1, 1)),
])
def test_unsnapshotted_vat_conditions_cannot_change_before_review(ready, field, value):
    invoice = draft(ready)
    setattr(ready.regime, field, value)
    save_financial_fixture(ready.regime)
    with pytest.raises(ValidationError, match="DDV vira"):
        review(invoice)


def test_removed_reference_requirement_cannot_authorize_missing_reference(ready):
    ready.regime.zahteva_referenco = True
    save_financial_fixture(ready.regime)
    invoice = draft(ready, referenca_listine="IZJAVA-1", datum_reference=date(2026, 9, 1))
    ready.regime.zahteva_referenco = False
    save_financial_fixture(ready.regime)
    InvoiceLine.objects.filter(invoice=invoice).update(referenca_listine="", datum_reference=None)
    with pytest.raises(ValidationError, match="DDV vira"):
        review(invoice)


def test_legacy_draft_without_vat_source_evidence_fails_closed(ready):
    original = draft(ready)
    legacy = Invoice.objects.get(pk=original.pk)
    legacy.pk = None
    legacy.save()
    line = original.invoiceline_set.get()
    line.pk, line.invoice = None, legacy
    line.save()
    with pytest.raises(ValidationError, match="DDV vira"):
        review(legacy)


def test_no_filesystem_operations_inside_numbering_transaction(ready):
    invoice = draft(ready)
    proof = review(invoice)
    from django.core.files.storage import default_storage

    def guarded(original):
        def call(*args, **kwargs):
            assert not connection.in_atomic_block, "Filesystem access in numbering transaction"
            return original(*args, **kwargs)
        return call

    statements = []

    def capture(execute, sql, params, many, context):
        statements.append(sql)
        return execute(sql, params, many, context)

    with ExitStack() as stack:
        for name in ("open", "exists", "save", "delete"):
            stack.enter_context(patch.object(default_storage, name, side_effect=guarded(getattr(default_storage, name))))
        with connection.execute_wrapper(capture):
            assert potrdi_osnutek_st(invoice, pregled=proof).stevilka
    assert "BEGIN IMMEDIATE" in statements


def test_change_between_asset_preparation_and_lock_is_rejected(ready):
    invoice = draft(ready)
    proof = review(invoice)
    original = st_confirmation._assets

    def changed(*args):
        assets = original(*args)
        InvoiceLine.objects.filter(invoice=invoice).update(opis="Changed during preparation")
        return assets

    numbers = counters()
    with (patch("invoicing.st_confirmation._assets", side_effect=changed),
          pytest.raises(ValidationError, match="zastarel")):
        potrdi_osnutek_st(invoice, pregled=proof)
    invoice.refresh_from_db()
    assert invoice.stevilka is None and invoice.status == "OSNUTEK"
    assert counters() == numbers


def test_st_cannot_enter_np_or_correction_confirmation(ready):
    invoice = draft(ready)
    with patch("invoicing.services.shrani_nespremenljiv_asset", side_effect=AssertionError):
        for service in (potrdi_osnutek_np, potrdi_korekcijski_osnutek):
            with pytest.raises(ValueError):
                service(invoice)
    assert invoice.stevilka is None


def test_cross_invoice_review_unsaved_draft_and_nested_transaction_are_rejected(ready):
    invoice = draft(ready)
    other = draft(ready)
    proof = review(invoice)
    with pytest.raises(ValidationError, match="pregled"):
        potrdi_osnutek_st(other, pregled=proof)
    with pytest.raises(ValidationError, match="shranjen"):
        fingerprint_osnutka_st(Invoice())
    with transaction.atomic(), pytest.raises(ValidationError, match="zunaj transakcije"):
        potrdi_osnutek_st(invoice, pregled=proof)


def test_concurrent_double_submit_allocates_once(ready):
    invoice = draft(ready)
    proof = review(invoice)
    barrier = Barrier(2)
    original = st_confirmation._assets

    def prepare(*args):
        result = original(*args)
        barrier.wait(timeout=15)
        return result

    def confirm():
        connections.close_all()
        try:
            return potrdi_osnutek_st(invoice, pregled=proof).stevilka
        except ValidationError:
            return "rejected"
        finally:
            connections.close_all()

    with patch("invoicing.st_confirmation._assets", side_effect=prepare), ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm(), range(2)))
    assert sorted(results) == ["ST-2026-0001", "rejected"]
    assert NumberSequence.objects.get(company=ready.company, tip="ST", leto=2026).zadnja_stevilka == 1
