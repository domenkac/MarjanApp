"""First-run evidence and the production issuance boundary."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.db import connections, transaction
from django.utils import timezone

from archive.configuration import (
    ArchiveConfigurationError,
    verify_and_save_archive_roots,
)
from archive.models import ArchiveConfiguration, ArchivedFile
from companies import readiness
from companies.models import (
    Company,
    CompanySampleConfirmation,
    InitialSetupCompletion,
    NumberingVerification,
    NumberSequence,
    SamplePdfPreview,
)
from companies.services import (
    dodeli_naslednjo_stevilko,
    potrdi_ddv_nastavitve,
    potrdi_vzorec_podjetja,
    veljavna_potrditev_vzorca,
)
from core.models import AuditLogEntry
from delivery.models import DeliveryAttempt
from invoicing.models import Invoice
from invoicing.services import (
    potrdi_korekcijski_osnutek,
    potrdi_osnutek_np,
    pripravi_korekcijski_osnutek,
    pripravi_osnutek_np,
)
from tests.financial_fixtures import save_financial_fixture, update_financial_fixture
from tests.setup_helpers import (
    add_setup_issuers,
    approve_sample,
    preview_sample,
    verified_archive_configuration,
)
from tests.smtp_helpers import approve_smtp
from tests.ui import test_company_confirmations

setup_data = test_company_confirmations.confirmation_ui_data

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def five_issuers(setup_data, settings):
    settings.MARJAN_SMTP_HOST = "smtp.example.test"
    settings.MARJAN_SMTP_PORT = 587
    settings.MARJAN_SMTP_SECURITY = "STARTTLS"
    settings.MARJAN_SMTP_TIMEOUT_SECONDS = 10
    settings.MARJAN_KEYRING_SERVICE = "test-marjanapp"
    potrdi_ddv_nastavitve(setup_data.company)
    approve_sample(setup_data.company, setup_data.lease)
    readiness.verify_numbering(
        setup_data.company,
        np_last=42,
        st_last=7,
        st_year=timezone.localdate().year,
        explicit=True,
    )
    others = add_setup_issuers(setup_data.company, setup_data.lease)
    approve_smtp(setup_data.company.pk)
    verified_archive_configuration()
    return setup_data, others


def issue_np(data):
    draft = pripravi_osnutek_np(
        data.lease, date(2026, 9, 1), date(2026, 9, 30), date(2026, 10, 1)
    ).invoice
    return potrdi_osnutek_np(draft)


def test_approval_requires_successfully_rendered_preview(setup_data):
    potrdi_ddv_nastavitve(setup_data.company)
    with pytest.raises(ValueError, match="predogled"):
        potrdi_vzorec_podjetja(setup_data.company, setup_data.lease)
    assert not CompanySampleConfirmation.objects.exists()


def test_five_issuer_completion_has_audit_and_actual_evidence(five_issuers):
    data, _ = five_issuers
    state = readiness.setup_readiness()
    assert state.ready, state.messages
    completion = readiness.complete_initial_setup()
    assert len(completion.evidence["companies"]) == 5
    assert all(
        row["preview_id"] and row["numbering_verification_id"]
        for row in completion.evidence["companies"]
    )
    assert completion.evidence["archive"]["operations"] == ["write", "read", "delete"]
    assert AuditLogEntry.objects.filter(polje="initial_setup").count() == 1
    assert readiness.complete_initial_setup() == completion
    assert InitialSetupCompletion.objects.count() == 1
    assert not Invoice.objects.exists()
    assert not DeliveryAttempt.objects.exists()
    assert not ArchivedFile.objects.exists()
    assert (
        NumberSequence.objects.get(company=data.company, tip="NP").zadnja_stevilka == 42
    )


def test_missing_fifth_issuer_blocks_completion_and_numbering(five_issuers):
    data, others = five_issuers
    update_financial_fixture(Company.objects.filter(pk=others[-1].pk), aktivno=False)
    with pytest.raises(ValidationError, match="najmanj pet"):
        readiness.complete_initial_setup()
    with pytest.raises(ValidationError, match="Začetna nastavitev"):
        dodeli_naslednjo_stevilko(data.company, "NP", date(2026, 10, 1))
    assert (
        NumberSequence.objects.get(company=data.company, tip="NP").zadnja_stevilka == 42
    )


@pytest.mark.parametrize("change", ["amount", "template", "forged", "company", "lease"])
def test_stale_forged_and_cross_company_preview_is_rejected(
    five_issuers, settings, change
):
    data, others = five_issuers
    preview = preview_sample(data.company, data.lease)
    preview_id = preview.preview_id
    if change == "amount":
        data.item.znesek_neto_mesecno = "125.00"
        save_financial_fixture(data.item)
    elif change == "template":
        settings.PDF_TEMPLATE_VERSION = "changed"
    elif change == "forged":
        preview_id = uuid4()
    elif change == "company":
        preview_id = SamplePdfPreview.objects.filter(company=others[0]).first().pk
    else:
        foreign_lease = SamplePdfPreview.objects.filter(company=others[0]).first().lease
        with pytest.raises(ValueError, match="ne pripada"):
            potrdi_vzorec_podjetja(data.company, foreign_lease, preview_id=preview_id)
        return
    before = CompanySampleConfirmation.objects.count()
    with pytest.raises(ValueError, match="predogled"):
        potrdi_vzorec_podjetja(data.company, data.lease, preview_id=preview_id)
    assert CompanySampleConfirmation.objects.count() == before


def test_render_failure_records_no_proof_or_approval(setup_data, monkeypatch):
    potrdi_ddv_nastavitve(setup_data.company)

    def fail(*args):
        raise OSError("render failed")

    monkeypatch.setattr("rendering.services._generiraj_pdf_bytes", fail)
    with pytest.raises(OSError):
        preview_sample(setup_data.company, setup_data.lease)
    assert not SamplePdfPreview.objects.exists()
    assert not CompanySampleConfirmation.objects.exists()


def test_legacy_approval_is_preserved_but_insufficient(five_issuers):
    data, _ = five_issuers
    confirmation = CompanySampleConfirmation.objects.get(company=data.company)
    CompanySampleConfirmation.objects.filter(pk=confirmation.pk).update(preview=None)
    assert veljavna_potrditev_vzorca(data.company).stanje == "valid"
    with pytest.raises(ValidationError, match="Potrditev vzorca"):
        readiness.complete_initial_setup()
    assert CompanySampleConfirmation.objects.filter(pk=confirmation.pk).exists()
    approve_sample(data.company, data.lease)
    readiness.complete_initial_setup()
    assert CompanySampleConfirmation.objects.filter(company=data.company).count() == 2


def test_numbering_requires_explicit_verification_not_existing_counters(five_issuers):
    data, _ = five_issuers
    NumberingVerification.objects.filter(company=data.company).delete()
    with pytest.raises(ValidationError, match="Izrecno preverite"):
        readiness.complete_initial_setup()
    with pytest.raises(ValidationError, match="Izrecno potrdite"):
        readiness.verify_numbering(data.company, np_last=42, st_last=7, st_year=2026)
    with pytest.raises(ValidationError, match="nižja"):
        readiness.verify_numbering(
            data.company, np_last=41, st_last=7, st_year=2026, explicit=True
        )


@pytest.mark.parametrize("location", ["local_root", "icloud_root", "dropbox_root"])
def test_each_archive_failure_blocks_initial_completion(
    five_issuers, monkeypatch, location
):
    config = ArchiveConfiguration.objects.get()
    original = readiness.archive_verification_evidence()

    def fail(root, label):
        if str(root) == getattr(config, location):
            raise ArchiveConfigurationError(
                f"{label} ni dostopen za varen zapis in branje."
            )

    monkeypatch.setattr("archive.configuration._verify_root", fail)
    ArchiveConfiguration.objects.all().delete()
    with pytest.raises(ArchiveConfigurationError):
        verify_and_save_archive_roots(
            config.local_root, config.icloud_root, config.dropbox_root
        )
    with pytest.raises(ValidationError, match="Lokacije"):
        readiness.complete_initial_setup()
    assert original and not InitialSetupCompletion.objects.exists()


def test_changed_roots_and_legacy_archive_timestamp_are_insufficient(five_issuers):
    ArchiveConfiguration.objects.update(verification_evidence={})
    with pytest.raises(ValidationError, match="Ponovno preverite"):
        readiness.complete_initial_setup()


def test_ui_resume_restart_and_repeated_finish_recompute_current_data(
    five_issuers, client
):
    from ui.models import SetupNavigation

    data, _ = five_issuers
    url = "/zacetna-nastavitev/"
    client.get(url)
    SetupNavigation.objects.update(
        company=data.company, step="issuer", lease=data.lease
    )
    assert client.post(url, {"action": "finish"}).status_code == 302
    assert client.post(url, {"action": "restart"}).status_code == 302
    assert SetupNavigation.objects.get().step == "company"
    assert InitialSetupCompletion.objects.count() == 1
    data.company.trr = "invalid"
    save_financial_fixture(data.company)
    response = client.post(url, {"action": "finish"})
    assert response.status_code == 200 and "IBAN" in response.content.decode()
    with pytest.raises(ValidationError, match="IBAN"):
        dodeli_naslednjo_stevilko(data.company, "NP", date(2026, 10, 1))


def test_direct_and_correction_issuance_recheck_each_company(five_issuers):
    data, others = five_issuers
    with pytest.raises(ValidationError, match="Začetna nastavitev"):
        issue_np(data)
    readiness.complete_initial_setup()
    original = issue_np(data)
    assert original.stevilka == "NP-00043"
    correction = pripravi_korekcijski_osnutek(
        original, Invoice.DocumentType.STORNO, "Popravek", date(2026, 10, 2)
    )
    update_financial_fixture(Company.objects.filter(pk=data.company.pk), naziv="Novo ime")
    with pytest.raises(ValidationError, match="Potrditev vzorca"):
        potrdi_korekcijski_osnutek(correction)
    correction.refresh_from_db()
    assert correction.stevilka is None
    assert (
        NumberSequence.objects.get(company=data.company, tip="NP").zadnja_stevilka == 43
    )
    approve_sample(data.company, data.lease)
    assert potrdi_korekcijski_osnutek(correction).stevilka == "NP-00044"
    # Another issuer's approval remains independently valid.
    assert readiness.setup_readiness(company=others[0]).ready


def test_allocation_and_completion_really_roll_back(five_issuers):
    data, _ = five_issuers
    with pytest.raises(RuntimeError), transaction.atomic():
        readiness.complete_initial_setup()
        assert dodeli_naslednjo_stevilko(data.company, "NP", date(2026, 10, 1)) == 43
        raise RuntimeError("rollback")
    assert not InitialSetupCompletion.objects.exists()
    assert not AuditLogEntry.objects.filter(polje="initial_setup").exists()
    assert (
        NumberSequence.objects.get(company=data.company, tip="NP").zadnja_stevilka == 42
    )


@pytest.mark.parametrize("complete", [False, True])
def test_concurrent_first_allocations_use_real_immediate_transactions(
    five_issuers, complete
):
    data, _ = five_issuers
    if complete:
        readiness.complete_initial_setup()
    barrier = Barrier(2)

    def allocate(_):
        connection = connections["default"]
        connection.close()
        connection.connect()
        identity = id(connection.connection)
        barrier.wait(timeout=10)
        try:
            try:
                return dodeli_naslednjo_stevilko(
                    data.company, "NP", date(2026, 10, 1)
                ), identity
            except ValidationError:
                return None, identity
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(allocate, range(2)))
    assert len({identity for _, identity in results}) == 2
    assert sorted(number for number, _ in results if number is not None) == (
        [43, 44] if complete else []
    )
    assert NumberSequence.objects.get(
        company=data.company, tip="NP"
    ).zadnja_stevilka == (44 if complete else 42)


def test_later_icloud_outage_does_not_probe_again_or_block_numbering(
    five_issuers, monkeypatch
):
    data, _ = five_issuers
    readiness.complete_initial_setup()

    def fail(*args, **kwargs):
        raise AssertionError("Archive probes must not repeat per invoice")

    monkeypatch.setattr("archive.configuration._verify_root", fail)
    monkeypatch.setattr(readiness, "validate_archive_locations", fail)
    from pathlib import Path

    Path(ArchiveConfiguration.objects.get().icloud_root).rmdir()
    assert issue_np(data).stevilka == "NP-00043"


def test_extra_active_issuer_is_checked_without_a_name_or_id_list(five_issuers):
    Company.objects.create(naziv="Šesti izdajatelj", davcna_stevilka="35012554")
    with pytest.raises(ValidationError, match="Šesti izdajatelj"):
        readiness.complete_initial_setup()


def test_forged_completion_flag_and_numbering_evidence_fail_closed(five_issuers):
    data, _ = five_issuers
    InitialSetupCompletion.objects.create(pk=1, evidence={"complete": True})
    with pytest.raises(ValidationError, match="Začetna nastavitev"):
        dodeli_naslednjo_stevilko(data.company, "NP", date(2026, 10, 1))
    InitialSetupCompletion.objects.all().delete()
    NumberingVerification.objects.filter(company=data.company).update(np_last=41)
    with pytest.raises(ValidationError, match="revizijsko dokazilo"):
        readiness.complete_initial_setup()


def test_actual_allocation_rechecks_after_outer_validation(five_issuers, monkeypatch):
    data, _ = five_issuers
    readiness.complete_initial_setup()
    allocate = dodeli_naslednjo_stevilko

    def change_then_allocate(company, tip, issue_date):
        update_financial_fixture(Company.objects.filter(pk=company.pk), naziv="Spremenjeno med izdajo")
        return allocate(company, tip, issue_date)

    monkeypatch.setattr(
        "invoicing.services.dodeli_naslednjo_stevilko", change_then_allocate
    )
    with pytest.raises(ValidationError, match="Potrditev vzorca"):
        issue_np(data)
    assert Invoice.objects.get().stevilka is None
    assert (
        NumberSequence.objects.get(company=data.company, tip="NP").zadnja_stevilka == 42
    )
    data.company.refresh_from_db()
    assert data.company.naziv == "Alfa d.o.o."


def test_forged_browser_token_cannot_approve_preview(five_issuers, client):
    from django.test import Client

    data, _ = five_issuers
    base = f"/nastavitve/podjetja/{data.company.pk}/"
    response = client.get(base + f"vzorec/{data.lease.pk}/pdf/")
    assert response.status_code == 200
    key = f"sample_preview_{data.company.pk}_{data.lease.pk}"
    other_browser = Client()
    other_browser.cookies[key] = str(
        SamplePdfPreview.objects.filter(company=data.company).latest("rendered_at").pk
    )
    before = CompanySampleConfirmation.objects.count()
    assert (
        other_browser.post(
            base + "potrdi-vzorec/",
            {"lease": data.lease.pk, "preview_id": other_browser.cookies[key].value},
        ).status_code
        == 409
    )
    assert CompanySampleConfirmation.objects.count() == before
    assert (
        client.post(base + "potrdi-vzorec/", {"lease": data.lease.pk}).status_code
        == 302
    )


def test_completion_audit_failure_rolls_back_and_can_resume(five_issuers, monkeypatch):
    audit = readiness._audit

    def fail(*args):
        raise RuntimeError("audit failure")

    monkeypatch.setattr(readiness, "_audit", fail)
    with pytest.raises(RuntimeError, match="audit failure"):
        readiness.complete_initial_setup()
    assert not InitialSetupCompletion.objects.exists()
    monkeypatch.setattr(readiness, "_audit", audit)
    assert readiness.complete_initial_setup().pk == 1


def test_rendered_content_proof_is_checked_even_with_matching_source(five_issuers):
    data, _ = five_issuers
    preview = preview_sample(data.company, data.lease)
    SamplePdfPreview.objects.filter(pk=preview.preview_id).update(
        content_fingerprint="0" * 64
    )
    with pytest.raises(ValueError, match="Vsebina predogleda je zastarela"):
        potrdi_vzorec_podjetja(data.company, data.lease, preview_id=preview.preview_id)


def test_initial_st_year_cannot_guess_current_historical_numbers(five_issuers):
    data, _ = five_issuers
    readiness.verify_numbering(
        data.company, np_last=42, st_last=0, st_year=2025, explicit=True
    )
    with pytest.raises(ValidationError, match="tekoče leto"):
        readiness.complete_initial_setup()


def test_numbering_form_rejects_a_different_displayed_company(five_issuers, client):
    from ui.models import SetupNavigation

    data, others = five_issuers
    client.get("/zacetna-nastavitev/")
    SetupNavigation.objects.update(company=data.company, step="issuer")
    before = NumberingVerification.objects.count()
    payload = {
        "action": "verify_numbering",
        "company_id": others[0].pk,
        "np_last": 42,
        "st_last": 7,
        "st_year": 2026,
        "explicit": "on",
    }
    response = client.post("/zacetna-nastavitev/", payload)
    assert (
        response.status_code == 200
        and "Izbrano podjetje se je spremenilo" in response.content.decode()
    )
    assert NumberingVerification.objects.count() == before
    payload["company_id"] = data.company.pk
    assert client.post("/zacetna-nastavitev/", payload).status_code == 302
    assert NumberingVerification.objects.count() == before + 1


def test_counter_cannot_fall_behind_issued_history(five_issuers):
    data, _ = five_issuers
    readiness.complete_initial_setup()
    issue_np(data)
    NumberSequence.objects.filter(company=data.company, tip="NP").update(
        zadnja_stevilka=42
    )
    with pytest.raises(ValidationError, match="števec je nižji"):
        dodeli_naslednjo_stevilko(data.company, "NP", date(2026, 10, 1))
    assert (
        NumberSequence.objects.get(company=data.company, tip="NP").zadnja_stevilka == 42
    )


def test_later_st_year_with_existing_history_requires_explicit_start(five_issuers):
    data, _ = five_issuers
    readiness.complete_initial_setup()
    Invoice.objects.create(
        company=data.company,
        document_type="ST",
        status="POSLAN",
        stevilka="ST-2027-0010",
        datum_izdaje=date(2027, 2, 1),
        obdobje_od=date(2027, 1, 1),
        obdobje_do=date(2027, 1, 31),
    )
    with pytest.raises(ValidationError, match="Preverite številčenje ST"):
        dodeli_naslednjo_stevilko(data.company, "ST", date(2027, 2, 1))
    assert not NumberSequence.objects.filter(
        company=data.company, tip="ST", leto=2027
    ).exists()
    readiness.verify_numbering(
        data.company, np_last=42, st_last=10, st_year=2027, explicit=True
    )
    assert dodeli_naslednjo_stevilko(data.company, "ST", date(2027, 2, 1)) == 11


def test_oversized_start_is_rejected_without_numbering_changes(five_issuers):
    data, _ = five_issuers
    before = list(NumberSequence.objects.values())
    with pytest.raises(ValidationError, match="dovoljeno velikost"):
        readiness.verify_numbering(
            data.company, np_last=10**30, st_last=7, st_year=2026, explicit=True
        )
    assert list(NumberSequence.objects.values()) == before
