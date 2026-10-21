"""Real storage boundaries for official numbering; synthetic files and SQLite only."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import date
from decimal import Decimal
from threading import Barrier, Event
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import connection, connections, transaction
from django.utils import timezone

from archive.models import ArchiveConfiguration
from companies import issuance_assets
from companies.models import (
    Company,
    InitialSetupCompletion,
    NumberingVerification,
    NumberSequence,
    Signatory,
)
from delivery.models import SmtpVerification
from invoicing import services, st_confirmation
from invoicing.models import Invoice, InvoiceLine, MonthlyCandidateReview
from tests.business_fixtures import update_business_fixture
from tests.financial_fixtures import update_financial_fixture
from tests.invoicing.test_preflight import (
    _freeze_ready_package,
)
from tests.invoicing.test_preflight import (
    ready_company as ready_company,  # noqa: PLC0414
)
from tests.setup_helpers import prepare_initial_setup

pytestmark = pytest.mark.django_db(transaction=True)
PATHS = ["np", "monthly", "np-storno", "np-credit", "st", "st-storno", "st-credit"]


def issue_action(data, path):
    if path == "monthly":
        candidate, _ = _freeze_ready_package(data)
        return lambda: services.materializiraj_zamrznjenega_mesecnega_kandidata(
            data["batch"].pk, data["lease"].pk, candidate["candidate_fingerprint"],
        )[0]
    prepare_initial_setup(data["company"], data["lease"])
    if path.startswith("np"):
        invoice = services.pripravi_osnutek_np(
            data["lease"], date(2026, 8, 1), date(2026, 8, 31), date(2026, 9, 1),
        ).invoice
        action = lambda: services.potrdi_osnutek_np(invoice)
    else:
        invoice = services.pripravi_osnutek_st(
            company=data["company"], prejemnik=data["tenant"], vat_regime=data["regime"],
            datum_izdaje=date(2026, 9, 1), obdobje_od=date(2026, 8, 1),
            obdobje_do=date(2026, 8, 31), bruto=Decimal("122.00"), opis="Storitev", enota_mere="ura",
        )
        proof = services.preglej_osnutek_st(
            invoice, expected_fingerprint=services.fingerprint_osnutka_st(invoice),
            individual_review=True,
        )
        action = lambda: services.potrdi_osnutek_st(invoice, pregled=proof)
    if "-" not in path:
        return action
    original = action()
    correction = services.pripravi_korekcijski_osnutek(
        original, "STORNO" if path.endswith("storno") else "DOBROPIS",
        "Ročno pregledan popravek", date(2026, 9, 2),
    )
    if path.endswith("credit"):
        source = original.invoiceline_set.first()
        services.dodaj_vrstico(
            correction, izvirna_vrstica=source, opis=source.opis,
            osnova=Decimal("-10.00"), ddv_znesek=Decimal("-2.20"), skupaj=Decimal("-12.20"),
            ddv_stopnja=source.ddv_stopnja, ddv_nacin_obracuna=source.ddv_nacin_obracuna,
            ddv_eslog_koda=source.ddv_eslog_koda, ddv_klavzula=source.ddv_klavzula,
            ddv_rekap_skupina=source.ddv_rekap_skupina,
        )
    return lambda: services.potrdi_korekcijski_osnutek(correction)


@pytest.mark.parametrize("path", PATHS)
def test_storage_stays_outside_numbering_transaction(ready_company, path):
    action = issue_action(ready_company, path)
    operations, statements = [], []

    def observe(name, original):
        def call(*args, **kwargs):
            operations.append((name, str(args[0]), connection.in_atomic_block))
            return original(*args, **kwargs)
        return call

    def capture(execute, sql, params, many, context):
        statements.append(sql)
        return execute(sql, params, many, context)

    with ExitStack() as stack:
        for name in ("open", "exists", "save", "delete"):
            stack.enter_context(patch.object(
                default_storage, name, side_effect=observe(name, getattr(default_storage, name)),
            ))
        with connection.execute_wrapper(capture):
            assert action().stevilka
    assert "BEGIN IMMEDIATE" in statements
    assert operations, "Expected actual asset verification"
    assert not [operation for operation in operations if operation[2]], operations


MUTATIONS = [
    "logo-name", "logo-checksum", "signature-name", "signature-checksum", "signatory",
    "validity", "company", "smtp", "setup", "sample", "numbering", "archive", "draft",
]


def mutate(data, name, settings):
    company = Company.objects.filter(pk=data["company"].pk)
    signatory = Signatory.objects.filter(company=data["company"])
    if name == "logo-name":
        update_financial_fixture(company, logotip="other.png")
    elif name == "logo-checksum":
        update_financial_fixture(company, logotip_checksum="0" * 64)
    elif name == "signature-name":
        update_business_fixture(signatory, podpis_slika="other.png")
    elif name == "signature-checksum":
        update_business_fixture(signatory, podpis_checksum="0" * 64)
    elif name == "signatory":
        update_business_fixture(signatory, ime_priimek="Drug podpisnik")
    elif name == "validity":
        update_business_fixture(signatory, velja_do=date(2026, 1, 2))
    elif name == "company":
        update_financial_fixture(company, rok_placila_dni_privzeto=90)
    elif name == "smtp":
        SmtpVerification.objects.filter(company_id=data["company"].pk).update(invalidated_at=timezone.now())
    elif name == "setup":
        InitialSetupCompletion.objects.update(evidence={})
    elif name == "sample":
        data["company"].sample_confirmations.all().delete()
    elif name == "numbering":
        NumberingVerification.objects.filter(company=data["company"]).delete()
    elif name == "archive":
        ArchiveConfiguration.objects.update(local_root="changed")
    elif Invoice.objects.filter(stevilka__isnull=True).exists():
        InvoiceLine.objects.filter(invoice__stevilka__isnull=True).update(opis="Spremenjen osnutek")
    else:
        MonthlyCandidateReview.objects.update(selected=False, non_issue_reason="Sprememba")


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("mutation", MUTATIONS)
def test_change_after_preflight_rejects_before_allocation(ready_company, settings, path, mutation):
    action = issue_action(ready_company, path)
    before = list(NumberSequence.objects.order_by("pk").values())
    original = issuance_assets.prepare_issuance_assets

    def changed(*args, **kwargs):
        proof = original(*args, **kwargs)
        mutate(ready_company, mutation, settings)
        return proof

    with (patch.object(services, "prepare_issuance_assets", side_effect=changed),
          patch.object(st_confirmation, "prepare_issuance_assets", side_effect=changed),
          pytest.raises((ValidationError, ValueError))):
        action()
    # Readiness invalidations may reach the allocator, but never change its counter.
    assert list(NumberSequence.objects.order_by("pk").values()) == before


@pytest.mark.parametrize("forgery", [None, {}, {"logo.png": "nespremenljivi-asseti/" + "0" * 64}, issuance_assets.IssuanceAssets("forged")])
def test_missing_or_forged_proof_under_lock_never_touches_storage(ready_company, forgery):
    from companies.services import dodeli_naslednjo_stevilko

    prepare_initial_setup(ready_company["company"], ready_company["lease"])
    with ExitStack() as stack:
        for name in ("open", "exists", "save", "delete"):
            stack.enter_context(patch.object(default_storage, name, side_effect=AssertionError(name)))
        with transaction.atomic(), pytest.raises(ValidationError):
            dodeli_naslednjo_stevilko(ready_company["company"], "NP", date(2026, 9, 1), asset_evidence=forgery)
    assert NumberSequence.objects.get(company=ready_company["company"], tip="NP").zadnja_stevilka == 0


@pytest.mark.parametrize("path", PATHS)
def test_issuance_rejects_nested_transaction_before_storage(ready_company, path):
    action = issue_action(ready_company, path)
    before = list(NumberSequence.objects.order_by("pk").values())
    with ExitStack() as stack:
        for name in ("open", "exists", "save", "delete"):
            stack.enter_context(patch.object(default_storage, name, side_effect=AssertionError(name)))
        with transaction.atomic(), pytest.raises(ValidationError, match="zunaj transakcije"):
            action()
    assert list(NumberSequence.objects.order_by("pk").values()) == before


@pytest.mark.parametrize("damage", ["missing-source", "wrong-checksum", "corrupt-immutable"])
def test_preflight_integrity_failure_preserves_number(ready_company, settings, damage):
    action = issue_action(ready_company, "np")
    company = Company.objects.get(pk=ready_company["company"].pk)
    if damage == "missing-source":
        (settings.MEDIA_ROOT / company.logotip.name).unlink()
    elif damage == "wrong-checksum":
        update_financial_fixture(Company.objects.filter(pk=company.pk), logotip_checksum="0" * 64)
    else:
        (settings.MEDIA_ROOT / f"nespremenljivi-asseti/{company.logotip_checksum}").write_bytes(b"corrupt")
    with pytest.raises((ValidationError, ValueError)):
        action()
    assert NumberSequence.objects.get(company=company, tip="NP").zadnja_stevilka == 0


@pytest.mark.parametrize("path", ["np", "st", "monthly"])
def test_concurrent_duplicate_requests_allocate_once(ready_company, path):
    action = issue_action(ready_company, path)
    barrier = Barrier(2)
    original = issuance_assets.prepare_issuance_assets

    def prepare(*args, **kwargs):
        proof = original(*args, **kwargs)
        barrier.wait(timeout=20)
        return proof

    def confirm():
        connections.close_all()
        try:
            connections["default"].ensure_connection()
            identity = id(connections["default"].connection)
            try:
                result = action().stevilka
            except (ValidationError, ValueError):
                result = "rejected"
            return result, identity
        finally:
            connections.close_all()

    with (patch.object(services, "prepare_issuance_assets", side_effect=prepare),
          patch.object(st_confirmation, "prepare_issuance_assets", side_effect=prepare),
          ThreadPoolExecutor(max_workers=2) as pool):
        results = list(pool.map(lambda _: confirm(), range(2)))
    expected = "ST-2026-0001" if path == "st" else "NP-00001"
    assert {result for result, _ in results} <= {expected, "rejected"}
    assert len({identity for _, identity in results}) == 2
    assert Invoice.objects.filter(stevilka=expected).count() == 1
    assert NumberSequence.objects.get(company=ready_company["company"], tip="ST" if path == "st" else "NP").zadnja_stevilka == 1


def test_slow_storage_preflight_does_not_hold_sqlite_writer(ready_company):
    action = issue_action(ready_company, "np")
    entered, release = Event(), Event()
    original = default_storage.open
    operations = []

    def slow(*args, **kwargs):
        operations.append(connection.in_atomic_block)
        assert not connection.in_atomic_block
        entered.set()
        assert release.wait(timeout=20)
        return original(*args, **kwargs)

    def confirm():
        connections.close_all()
        try:
            return action()
        finally:
            connections.close_all()

    with patch.object(default_storage, "open", side_effect=slow), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(confirm)
        try:
            assert entered.wait(timeout=20)
            # A real competing IMMEDIATE writer finishes while disk is blocked.
            with transaction.atomic():
                NumberSequence.objects.filter(company=ready_company["company"], tip="ST").update(zadnja_stevilka=0)
        finally:
            release.set()
        assert future.result(timeout=30).stevilka == "NP-00001"
    assert operations and not any(operations)


def test_direct_issuance_wins_while_monthly_preflight_is_delayed(ready_company):
    monthly = issue_action(ready_company, "monthly")
    entered, release = Event(), Event()
    original = issuance_assets.prepare_issuance_assets

    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("frozen_paths"):
            assert not connection.in_atomic_block
            entered.set()
            assert release.wait(timeout=20)
        return result

    def materialize():
        connections.close_all()
        try:
            try:
                return monthly().stevilka
            except ValidationError:
                return "conflict"
        finally:
            connections.close_all()

    with patch.object(services, "prepare_issuance_assets", side_effect=prepare), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(materialize)
        try:
            assert entered.wait(timeout=20)
            draft = services.pripravi_osnutek_np(
                ready_company["lease"], date(2026, 9, 1), date(2026, 9, 30), date(2026, 9, 10),
            ).invoice
            direct = services.potrdi_osnutek_np(draft)
            assert direct.stevilka == "NP-00001"
        finally:
            release.set()
        assert future.result(timeout=30) == "conflict"
    assert Invoice.objects.filter(stevilka__isnull=False).count() == 1
    assert NumberSequence.objects.get(company=ready_company["company"], tip="NP").zadnja_stevilka == 1


def test_frozen_asset_bytes_are_verified_before_numbering(ready_company, settings):
    action = issue_action(ready_company, "monthly")
    frozen = ready_company["batch"].calculation_snapshot
    immutable = settings.MEDIA_ROOT / f"nespremenljivi-asseti/{frozen['company']['logo_checksum']}"
    immutable.write_bytes(b"corrupted frozen logo")
    with pytest.raises((ValidationError, ValueError)):
        action()
    assert not Invoice.objects.exists()
    assert NumberSequence.objects.get(company=ready_company["company"], tip="NP").zadnja_stevilka == 0


def test_asset_bytes_changed_after_preflight_are_rejected_by_pdf_gate(ready_company, settings):
    """A DB lock cannot prevent external file changes; rendering still checks hashes."""
    from rendering.contexts import PdfRenderNapaka, pripravi_pdf_context

    action = issue_action(ready_company, "st")
    original = issuance_assets.prepare_issuance_assets

    def prepare(*args, **kwargs):
        proof = original(*args, **kwargs)
        for relative in proof.references.values():
            (settings.MEDIA_ROOT / relative).write_bytes(b"external mutation")
        return proof

    with patch.object(st_confirmation, "prepare_issuance_assets", side_effect=prepare):
        issued = action()
    assert issued.stevilka == "ST-2026-0001"
    with pytest.raises(PdfRenderNapaka):
        pripravi_pdf_context(issued)


def test_sealed_proof_cannot_be_substituted_between_companies_or_dates(ready_company):
    from companies.services import dodeli_naslednjo_stevilko

    prepare_initial_setup(ready_company["company"], ready_company["lease"])
    company = ready_company["company"]
    proof = issuance_assets.prepare_issuance_assets(company, date(2026, 9, 1))
    with pytest.raises(ValidationError, match="Vir asseta"):
        proof["not-the-verified-source.png"]
    other = Company.objects.exclude(pk=company.pk).first()
    for issuer, issue_date in ((company, date(2026, 9, 2)), (other, date(2026, 9, 1))):
        with pytest.raises(ValidationError):
            dodeli_naslednjo_stevilko(issuer, "NP", issue_date, asset_evidence=proof)
    tampered = issuance_assets.IssuanceAssets(proof.seal + "tampered")
    with pytest.raises(ValidationError):
        dodeli_naslednjo_stevilko(company, "NP", date(2026, 9, 1), asset_evidence=tampered)
    assert NumberSequence.objects.get(company=company, tip="NP").zadnja_stevilka == 0


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("phase", ["before-number", "after-number", "after-save"])
def test_failure_rolls_back_and_retry_uses_next_number(ready_company, path, phase):
    action = issue_action(ready_company, path)
    numbers = list(NumberSequence.objects.order_by("pk").values())
    invoices = list(Invoice.objects.order_by("pk").values())
    lines = list(InvoiceLine.objects.order_by("pk").values())
    allocate = services.dodeli_naslednjo_stevilko
    save = Invoice.save

    def fail_number(*args, **kwargs):
        if phase == "after-number":
            allocate(*args, **kwargs)
        raise RuntimeError("injected numbering failure")

    def fail_save(invoice, *args, **kwargs):
        save(invoice, *args, **kwargs)
        if invoice.stevilka:
            raise RuntimeError("injected snapshot failure")

    with ExitStack() as stack:
        if phase == "after-save":
            stack.enter_context(patch.object(Invoice, "save", fail_save))
        else:
            stack.enter_context(patch.object(services, "dodeli_naslednjo_stevilko", side_effect=fail_number))
            stack.enter_context(patch.object(st_confirmation, "dodeli_naslednjo_stevilko", side_effect=fail_number))
        with pytest.raises(RuntimeError, match="injected"):
            action()
    assert list(NumberSequence.objects.order_by("pk").values()) == numbers
    assert list(Invoice.objects.order_by("pk").values()) == invoices
    assert list(InvoiceLine.objects.order_by("pk").values()) == lines
    result = action()
    assert result.stevilka
    before_retry = list(NumberSequence.objects.order_by("pk").values())
    try:
        retry = action()
        assert retry.pk == result.pk and retry.stevilka == result.stevilka
    except (ValidationError, ValueError):
        pass
    assert list(NumberSequence.objects.order_by("pk").values()) == before_retry
