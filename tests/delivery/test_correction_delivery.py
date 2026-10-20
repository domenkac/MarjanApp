"""Correction operations use synthetic databases, files and SMTP only."""
# ruff: noqa: F401, F811 -- Imported pytest fixtures.

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from threading import Barrier, Event

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, models

from companies.models import NumberSequence
from core.models import AuditLogEntry
from delivery.models import DeliveryAttempt
from delivery.services import send_pdf_email
from invoicing.models import Invoice
from invoicing.services import (
    dodaj_vrstico,
    fingerprint_osnutka_st,
    potrdi_korekcijski_osnutek,
    potrdi_osnutek_st,
    preglej_osnutek_st,
    pripravi_korekcijski_osnutek,
    pripravi_osnutek_st,
)
from tests.delivery.test_delivery_services import FakeSmtp, smtp_config
from tests.invoicing.test_korekcije import izdan_np
from tests.invoicing.test_priprava_np import pogodba

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def correction_factory(pogodba, settings, tmp_path, monkeypatch):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    for target in ("socket.create_connection", "smtplib.SMTP", "smtplib.SMTP_SSL",
                   "keyring.get_password", "keyring.set_password"):
        monkeypatch.setattr(target, lambda *a, **k: pytest.fail("Real external access forbidden"))
    original_np = izdan_np(pogodba)

    def create(kind="STORNO", source="NP"):
        original = original_np
        if source == "standalone":
            original = pripravi_osnutek_st(
                company=original_np.company, prejemnik=pogodba.tenant,
                vat_regime=pogodba.leaseitem_set.first().vat_regime,
                datum_izdaje=date(2026, 9, 1), obdobje_od=date(2026, 8, 1),
                obdobje_do=date(2026, 8, 31), bruto=Decimal("122.00"),
                opis="Storitev", enota_mere="ura",
            )
            review = preglej_osnutek_st(original, expected_fingerprint=fingerprint_osnutka_st(original), individual_review=True)
            original = potrdi_osnutek_st(original, pregled=review)
        elif source == "legacy":
            values = Invoice.objects.values().get(pk=original.pk)
            values.pop("id")
            values.update(document_type="ST", stevilka="ST-2026-0001")
            original = Invoice.objects.create(**values)
            for line in original_np.invoiceline_set.all():
                fields = {f.attname: getattr(line, f.attname) for f in line._meta.concrete_fields if f.name not in {"id", "invoice"}}
                original.invoiceline_set.create(**fields)
        invoice = pripravi_korekcijski_osnutek(original, kind, "Napačen znesek", date(2027, 1, 2))
        if kind == "DOBROPIS":
            source_line = original.invoiceline_set.first()
            dodaj_vrstico(invoice, izvirna_vrstica=source_line, opis=source_line.opis,
                         osnova=Decimal("-10.00"), ddv_znesek=Decimal("-2.20"), skupaj=Decimal("-12.20"),
                         **{n: getattr(source_line, n) for n in ("ddv_stopnja", "ddv_nacin_obracuna", "ddv_eslog_koda", "ddv_rekap_skupina", "ddv_klavzula")})
        invoice = potrdi_korekcijski_osnutek(invoice)
        pdf = b"%PDF-1.7\nsynthetic official correction\n%%EOF"
        path = f"company-{invoice.company_id}/{invoice.stevilka}.pdf"
        artifact = settings.RENDERING_ROOT / path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(pdf)
        models.QuerySet.update(Invoice.objects.filter(pk=invoice.pk), rendered_pdf_path=path, checksum=sha256(pdf).hexdigest())
        invoice.refresh_from_db()
        return invoice
    return create


def prepare(invoice):
    from delivery.corrections import (
        prepare_correction_attempt,
        review_correction_delivery,
    )
    return prepare_correction_attempt(invoice, review=review_correction_delivery(
        invoice, recipients_to=["buyer@example.com"], recipients_cc=["copy@example.com"],
    ))


def send(attempt, smtp_config, fake=None):
    from delivery.corrections import send_correction_email
    return send_correction_email(attempt, smtp_config=replace(smtp_config, username=attempt.sender),
                                 credential_ref=object(), password_loader=lambda ref: "top-secret",
                                 smtp_factory=lambda *a: fake or FakeSmtp())


@pytest.mark.parametrize("kind", ["STORNO", "DOBROPIS"])
@pytest.mark.parametrize("source", ["NP", "standalone", "legacy"])
def test_prepare_send_exact_pdf_once_preserves_original_and_finances(correction_factory, smtp_config, settings, monkeypatch, kind, source):
    invoice = correction_factory(kind, source)
    for target in ("rendering.services.render_pdf", "rendering.services.render_eslog", "companies.services.dodeli_naslednjo_stevilko"):
        monkeypatch.setattr(target, lambda *a, **k: pytest.fail("Implicit issuance/rendering forbidden"))
    original = Invoice.objects.values().get(pk=invoice.povezan_racun_id)
    correction_before = Invoice.objects.values().get(pk=invoice.pk)
    financial = list(invoice.invoiceline_set.values())
    numbers = list(NumberSequence.objects.values())
    attempt = prepare(invoice)
    assert attempt.result == "PREPARED" and attempt.submission_started_at is None
    fake = FakeSmtp()
    assert send(attempt, smtp_config, fake).result == "ACCEPTED"
    with pytest.raises(ValidationError):
        send(attempt, smtp_config, fake)
    assert sum(call[0] == "data" for call in fake.calls) == 1
    message = BytesParser(policy=policy.default).parsebytes(next(call[1] for call in fake.calls if call[0] == "data"))
    assert message["Subject"].startswith("Storno" if kind == "STORNO" else "Dobropis")
    assert "Bcc" not in message and invoice.company.email_racunovodja not in str(message)
    assert next(message.iter_attachments()).get_payload(decode=True) == (settings.RENDERING_ROOT / attempt.pdf_path).read_bytes()
    assert Invoice.objects.values().get(pk=invoice.povezan_racun_id) == original
    correction_after = Invoice.objects.values().get(pk=invoice.pk)
    assert correction_after.pop("status") == "POSLAN"
    correction_before.pop("status")
    assert correction_after == correction_before
    assert financial == list(invoice.invoiceline_set.values()) and numbers == list(NumberSequence.objects.values())


@pytest.mark.parametrize("fault", ["missing", "tampered", "snapshot", "issuer", "original", "draft", "eslog"])
def test_invalid_correction_or_pdf_blocks_prepare_without_effects(correction_factory, settings, fault):
    invoice = correction_factory()
    if fault in {"missing", "tampered"}:
        artifact = settings.RENDERING_ROOT / invoice.rendered_pdf_path
        artifact.unlink() if fault == "missing" else artifact.write_bytes(b"damaged")
    else:
        changes = {"snapshot": {"prejemnik_naziv": "Wrong"}, "issuer": {"company_id": 999999},
                   "original": {"povezan_racun_id": invoice.pk}, "draft": {"status": "OSNUTEK"},
                   "eslog": {"rendered_eslog_path": "forbidden.xml"}}[fault]
        if fault == "issuer":
            # A real second issuer keeps SQLite referential integrity enabled.
            from companies.models import Company
            changes = {"company_id": Company.objects.create(naziv="Drugi", davcna_stevilka="99999999").pk}
        models.QuerySet.update(Invoice.objects.filter(pk=invoice.pk), **changes)
    with pytest.raises((ValidationError, ValueError)):
        prepare(invoice)
    assert not DeliveryAttempt.objects.exists()


@pytest.mark.parametrize("to,cc", [([], []), (["bad"], []), (["buyer@example.com\n"], []),
    (["buyer@example.com"], ["BUYER@example.com"]), (["racunovodja@example.com"], []),
    (["buyer@example.com"], ["racunovodja@example.com"])])
def test_recipient_validation_rejects_cross_role_duplicates(correction_factory, to, cc):
    from delivery.corrections import review_correction_delivery
    with pytest.raises(ValidationError):
        review_correction_delivery(correction_factory(), recipients_to=to, recipients_cc=cc)
    assert not DeliveryAttempt.objects.exists()


@pytest.mark.parametrize("fault", ["pdf", "sender", "smtp", "recipients", "identity"])
def test_send_revalidates_before_credentials_or_network(correction_factory, smtp_config, settings, fault):
    invoice = correction_factory()
    attempt = prepare(invoice)
    smtp_config = replace(smtp_config, username=attempt.sender)
    if fault == "pdf":
        (settings.RENDERING_ROOT / attempt.pdf_path).write_bytes(b"damaged")
    elif fault == "sender":
        models.QuerySet.update(type(invoice.company).objects.filter(pk=invoice.company_id), email_posiljatelj="other@example.com")
    elif fault == "smtp":
        smtp_config = replace(smtp_config, username="wrong@example.com")
    elif fault == "recipients":
        models.QuerySet.update(DeliveryAttempt.objects.all(), recipients_to=["racunovodja@example.com"])
    else:
        models.QuerySet.update(Invoice.objects.filter(pk=invoice.pk), razlog="Spremenjeno")
    with pytest.raises((ValidationError, ValueError)):
        send_pdf_email(attempt, subject="Storno", body="Storno", smtp_config=smtp_config,
                       credential_ref=object(), password_loader=lambda ref: pytest.fail("Credential access"),
                       smtp_factory=lambda *a: pytest.fail("SMTP access"))
    attempt.refresh_from_db()
    assert attempt.result == "PREPARED"


def test_confirmed_failure_retry_requires_reason_and_keeps_frozen_data(correction_factory, smtp_config):
    from delivery.corrections import correction_delivery_action
    invoice = correction_factory()
    source = send(prepare(invoice), smtp_config, FakeSmtp({"data": (550, b"rejected")}))
    before = DeliveryAttempt.objects.values().get(pk=source.pk)
    for reason in ("", "...", "   "):
        with pytest.raises(ValidationError):
            correction_delivery_action(source, action="retry", reason=reason)
    retry = correction_delivery_action(source, action="retry", reason="Odpravljena težava poštnega strežnika.")
    assert retry.result == "PREPARED" and retry.submission_started_at is None
    for name in ("sender", "recipients_to", "recipients_cc", "recipients_bcc", "pdf_path", "pdf_checksum"):
        assert getattr(retry, name) == getattr(source, name)
    assert DeliveryAttempt.objects.values().get(pk=source.pk) == before
    assert AuditLogEntry.objects.filter(polje="correction_retry", object_id=source.pk).count() == 1
    with pytest.raises(ValidationError):
        correction_delivery_action(source, action="retry", reason="Ponovljeno")
    assert send(retry, smtp_config).result == "ACCEPTED"


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("action", ["unknown_sent", "unknown_not_sent"])
def test_unknown_requires_explicit_resolution_preserves_history(correction_factory, smtp_config, interrupted, action):
    from delivery.corrections import correction_delivery_action
    from delivery.services import (
        mark_submission_started,
        recover_interrupted_submissions,
    )
    source = prepare(correction_factory())
    if interrupted:
        mark_submission_started(source)
        assert recover_interrupted_submissions(invoice_ids=[source.invoice_id]) == 1
        source.refresh_from_db()
    else:
        source = send(source, smtp_config, FakeSmtp({"data": TimeoutError()}))
    assert source.result == "UNKNOWN"
    before = DeliveryAttempt.objects.values().get(pk=source.pk)
    with pytest.raises(ValidationError):
        send(source, smtp_config)
    with pytest.raises(ValidationError):
        correction_delivery_action(source, action="retry", reason="Neznano ni potrjen neuspeh.")
    with pytest.raises(ValidationError):
        correction_delivery_action(source, action=action, reason="")
    result = correction_delivery_action(source, action=action, reason="Ponudnik je izrecno potrdil rezultat oddaje.")
    assert DeliveryAttempt.objects.values().get(pk=source.pk) == before
    assert AuditLogEntry.objects.filter(polje="unknown_resolution").count() == 1
    with pytest.raises(ValidationError):
        correction_delivery_action(source, action=action, reason="Ponovljeno preverjanje")
    if action == "unknown_not_sent":
        assert result.result == "PREPARED" and result.submission_started_at is None
        assert send(result, smtp_config).result == "ACCEPTED"
    else:
        assert DeliveryAttempt.objects.count() == 1


def test_parallel_preparation_and_sending_have_one_winner(correction_factory, smtp_config):
    from delivery.corrections import (
        prepare_correction_attempt,
        review_correction_delivery,
    )
    from delivery.services import recover_interrupted_submissions
    invoice = correction_factory()
    review = review_correction_delivery(invoice, recipients_to=["buyer@example.com"])
    barrier = Barrier(2)
    def run_prepare():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return prepare_correction_attempt(invoice, review=review).pk
        except ValidationError:
            return None
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_prepare(), range(2)))
    assert sum(value is not None for value in results) == 1 and DeliveryAttempt.objects.count() == 1
    attempt = DeliveryAttempt.objects.get()
    active, release = Event(), Event()
    def data(payload):
        active.set()
        assert release.wait(timeout=10)
        return 250, b"queued"
    fake = FakeSmtp({"data": data})
    def run_send():
        close_old_connections()
        try:
            return send(attempt, smtp_config, fake)
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(run_send)
        try:
            assert active.wait(timeout=10)
            with pytest.raises(ValidationError):
                send(attempt, smtp_config, fake)
            assert recover_interrupted_submissions(invoice_ids=[invoice.pk]) == 0
        finally:
            release.set()
        assert first.result(timeout=10).result == "ACCEPTED"
    assert sum(call[0] == "data" for call in fake.calls) == 1


@pytest.mark.parametrize("unknown", [False, True])
def test_pdf_tampering_blocks_retry_without_new_evidence(correction_factory, smtp_config, settings, unknown):
    from delivery.corrections import correction_delivery_action
    invoice = correction_factory()
    source = send(prepare(invoice), smtp_config, FakeSmtp({"data": TimeoutError() if unknown else (550, b"rejected")}))
    (settings.RENDERING_ROOT / source.pdf_path).write_bytes(b"corrupt")
    audit = list(AuditLogEntry.objects.values())
    with pytest.raises(ValidationError):
        correction_delivery_action(source, action="unknown_not_sent" if unknown else "retry", reason="Preverjeno neposlano.")
    assert DeliveryAttempt.objects.count() == 1 and audit == list(AuditLogEntry.objects.values())


def test_shared_composer_never_labels_correction_as_ordinary_invoice(correction_factory):
    from delivery.services import build_pdf_email
    invoice = correction_factory()
    email = build_pdf_email(prepare(invoice), subject="Račun", body="V prilogi je račun.")
    assert email.message["Subject"] == f"Storno {invoice.stevilka}"
    assert invoice.povezan_racun.stevilka in email.message.get_body().get_content()


def test_original_pdf_registration_and_delivery_do_not_change_correction_identity(correction_factory, smtp_config):
    invoice = correction_factory()
    attempt = prepare(invoice)
    original = invoice.povezan_racun
    models.QuerySet.update(Invoice.objects.filter(pk=original.pk), status="POSLAN",
        rendered_pdf_path=f"company-{original.company_id}/{original.stevilka}.pdf", checksum="c" * 64)
    assert send(attempt, smtp_config).result == "ACCEPTED"
