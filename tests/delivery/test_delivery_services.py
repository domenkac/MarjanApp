import traceback
from datetime import date
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from hashlib import sha256

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import connection

from companies.models import Company, NumberSequence
from contracts.models import Lease, Property, RecipientContact, Tenant, Unit
from core.models import AuditLogEntry
from delivery.credentials import CredentialError, KeyringCredentialRef, load_password
from delivery.models import DeliveryAttempt
from delivery.services import (
    UNKNOWN_RESOLUTION_FIELD,
    UNKNOWN_RESOLUTION_NOT_SENT_CONFIRMED,
    UNKNOWN_RESOLUTION_SENT_VERIFIED,
    UNKNOWN_RESOLUTION_UNRESOLVED,
    build_pdf_email,
    is_retry_eligible,
    mark_accepted,
    mark_confirmed_failure,
    mark_result_unknown,
    mark_submission_started,
    prepare_attempt,
    prepare_retry_attempt,
    resolve_unknown_as_sent,
    resolve_unknown_user_confirmed_not_sent_and_prepare_retry,
    send_pdf_email,
)
from delivery.transport import (
    SmtpConfig,
    SmtpSecurity,
)
from delivery.transport import (
    test_smtp_connection as check_smtp_connection,
)
from invoicing.models import Invoice, InvoiceLine, MonthlyCycle
from tests.business_fixtures import update_business_records
from tests.smtp_helpers import approve_smtp


@pytest.fixture
def issued_invoice(transactional_db, settings):
    settings.MARJAN_SMTP_HOST = "smtp.example.com"
    settings.MARJAN_SMTP_PORT = 587
    settings.MARJAN_SMTP_SECURITY = "STARTTLS"
    settings.MARJAN_SMTP_TIMEOUT_SECONDS = 12.5
    settings.MARJAN_KEYRING_SERVICE = "marjan"
    company = Company.objects.create(
        naziv="Alfa d.o.o.",
        davcna_stevilka="15012557",
        maticna_stevilka="1234567000",
        naslov="Glavna 1",
        drzava_koda="SI",
        trr="SI56191000000123438",
        logotip="logo.png",
        email_racunovodja="accountant@example.com",
        email_posiljatelj="sender@example.com",
        keychain_kljuc_posiljatelja="smtp-user",
    )
    property_ = Property.objects.create(
        company=company, naziv="Stavba", naslov="Glavna 1"
    )
    unit = Unit.objects.create(
        property=property_, oznaka="P-1", tip_enote=Unit.TipEnote.PISARNA
    )
    tenant = Tenant.objects.create(
        tip=Tenant.Tip.PRAVNA_OSEBA,
        naziv="Najemnik d.o.o.",
        davcna_stevilka="15012557",
        maticna_stevilka="1234567000",
        naslov="Trg 1",
        drzava_koda="SI",
        identificiran_za_ddv=True,
        email="tenant@example.com",
    )
    lease = Lease.objects.create(
        unit=unit,
        tenant=tenant,
        oznaka_pogodbe="P-2026",
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2026, 1, 1),
        rok_placila_dni=15,
    )
    RecipientContact.objects.create(
        lease=lease,
        email="to@example.com",
        vloga=RecipientContact.Vloga.TO,
        kanal=RecipientContact.Kanal.PDF,
    )
    RecipientContact.objects.create(
        lease=lease,
        email="cc@example.com",
        vloga=RecipientContact.Vloga.CC,
        kanal=RecipientContact.Kanal.OBOJE,
    )
    RecipientContact.objects.create(
        lease=lease,
        email="accountant@example.com",
        vloga=RecipientContact.Vloga.BCC,
        kanal=RecipientContact.Kanal.PDF,
    )
    invoice = Invoice.objects.create(
        company=company,
        lease=lease,
        document_type=Invoice.DocumentType.NP,
        stevilka="NP-00001",
        datum_izdaje=date(2026, 9, 1),
        obdobje_od=date(2026, 8, 1),
        obdobje_do=date(2026, 8, 31),
        status=Invoice.Status.IZBRAN_ZA_POSILJANJE,
        rendered_pdf_path="company-1/NP-00001.pdf",
        checksum="a" * 64,
        skupaj_z_ddv=Decimal("122.00"),
    )
    InvoiceLine.objects.create(
        invoice=invoice,
        lease=lease,
        opis="Najemnina",
        osnova=Decimal("100.00"),
        ddv_stopnja=Decimal("22.00"),
        ddv_znesek=Decimal("22.00"),
        skupaj=Decimal("122.00"),
        ddv_nacin_obracuna="OBDAVCENO",
        ddv_rekap_skupina="DDV 22 %",
    )
    NumberSequence.objects.create(
        company=company,
        tip=NumberSequence.Tip.NP,
        leto=0,
        zadnja_stevilka=1,
    )
    approve_smtp(company.pk)
    return invoice


@pytest.fixture
def prepared_email_data(issued_invoice, settings, tmp_path):
    pdf_bytes = b"%PDF-1.7\nverified official invoice\n%%EOF"
    path = f"company-{issued_invoice.company_id}/{issued_invoice.stevilka}.pdf"
    settings.RENDERING_ROOT = tmp_path / "rendered"
    artifact = settings.RENDERING_ROOT / path
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(pdf_bytes)
    checksum = sha256(pdf_bytes).hexdigest()
    Invoice.objects.filter(pk=issued_invoice.pk).update(
        rendered_pdf_path=path, checksum=checksum
    )
    issued_invoice.refresh_from_db()
    return issued_invoice, prepare_attempt(issued_invoice), artifact, pdf_bytes


@pytest.fixture
def smtp_config(settings):
    settings.MARJAN_SMTP_HOST = "smtp.example.com"
    settings.MARJAN_SMTP_PORT = 587
    settings.MARJAN_SMTP_SECURITY = "STARTTLS"
    settings.MARJAN_SMTP_TIMEOUT_SECONDS = 12.5
    settings.MARJAN_KEYRING_SERVICE = "marjan"
    return SmtpConfig(
        host="smtp.example.com",
        port=587,
        security=SmtpSecurity.STARTTLS,
        timeout_seconds=12.5,
        username="sender@example.com",
    )


class FakeSmtp:
    def __init__(self, behavior=None):
        self.behavior = behavior or {}
        self.calls = []

    def _call(self, name, *args):
        assert connection.in_atomic_block is False
        self.calls.append((name, *args))
        outcome = self.behavior.get(name)
        if callable(outcome):
            return outcome(*args)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def ehlo(self):
        return self._call("ehlo") or (250, b"hello")

    def starttls(self, *, context):
        assert context.verify_mode != 0
        return self._call("starttls") or (220, b"ready")

    def login(self, username, password):
        assert password == "top-secret"
        return self._call("login", username) or (235, b"ok")

    def mail(self, sender):
        return self._call("mail", sender) or (250, b"sender ok")

    def rcpt(self, recipient):
        outcomes = self.behavior.get("rcpt", {})
        self._call("rcpt_call", recipient)
        outcome = outcomes.get(recipient, (250, b"recipient ok"))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def data(self, payload):
        return self._call("data", payload) or (250, b"queued")

    def rset(self):
        return self._call("rset") or (250, b"reset")

    def quit(self):
        return self._call("quit") or (221, b"bye")

    def close(self):
        return self._call("close")


def smtp_factory(fake):
    def factory(config, context):
        assert connection.in_atomic_block is False
        assert (config.host, config.port, config.timeout_seconds) == (
            "smtp.example.com",
            587,
            12.5,
        )
        return fake

    return factory


def password_loader(ref):
    assert connection.in_atomic_block is False
    assert ref == KeyringCredentialRef("marjan", "smtp-user")
    return "top-secret"


def test_prepare_stores_sender_recipients_and_pdf_without_secret(issued_invoice):
    attempt = prepare_attempt(issued_invoice)

    assert attempt.sequence == 1
    assert attempt.sender == "sender@example.com"
    assert attempt.recipients_to == ["to@example.com"]
    assert attempt.recipients_cc == ["cc@example.com"]
    assert attempt.recipients_bcc == ["accountant@example.com"]
    assert (attempt.pdf_path, attempt.pdf_checksum) == (
        issued_invoice.rendered_pdf_path,
        issued_invoice.checksum,
    )
    assert "keychain-reference" not in str(attempt.__dict__)
    with pytest.raises(ValidationError):
        prepare_attempt(issued_invoice)


def test_attempt_model_allows_empty_optional_recipient_lists(issued_invoice):
    attempt = DeliveryAttempt(
        invoice=issued_invoice,
        sequence=1,
        sender="sender@example.com",
        recipients_to=["to@example.com"],
        recipients_cc=[],
        recipients_bcc=[],
        pdf_path=issued_invoice.rendered_pdf_path,
        pdf_checksum=issued_invoice.checksum,
    )

    attempt.full_clean()


def test_prepare_allows_no_cc_contact_and_keeps_accountant_bcc(issued_invoice):
    RecipientContact.objects.filter(
        lease=issued_invoice.lease,
        vloga=RecipientContact.Vloga.CC,
    ).delete()

    attempt = prepare_attempt(issued_invoice)

    assert attempt.recipients_to == ["to@example.com"]
    assert attempt.recipients_cc == []
    assert attempt.recipients_bcc == ["accountant@example.com"]


def test_prepare_rejects_missing_to_with_explicit_error(issued_invoice):
    RecipientContact.objects.filter(
        lease=issued_invoice.lease,
        vloga=RecipientContact.Vloga.TO,
    ).delete()

    with pytest.raises(ValidationError, match="At least one To recipient is required"):
        prepare_attempt(issued_invoice)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("recipients_to", "to@example.com"),
        ("recipients_to", [""]),
        ("recipients_cc", "cc@example.com"),
        ("recipients_cc", [""]),
        ("recipients_bcc", "bcc@example.com"),
        ("recipients_bcc", [""]),
    ],
)
def test_attempt_model_rejects_malformed_recipient_lists(issued_invoice, field, value):
    attempt = DeliveryAttempt(
        invoice=issued_invoice,
        sequence=1,
        sender="sender@example.com",
        recipients_to=["to@example.com"],
        recipients_cc=[],
        recipients_bcc=[],
        pdf_path=issued_invoice.rendered_pdf_path,
        pdf_checksum=issued_invoice.checksum,
    )
    setattr(attempt, field, value)

    with pytest.raises(ValidationError, match="list of non-empty email strings"):
        attempt.full_clean()


@pytest.mark.parametrize(
    ("status", "path", "checksum"),
    [
        (Invoice.Status.ARHIVIRAN_NI_IZDAN, "invoice.pdf", "a" * 64),
        (Invoice.Status.IZBRAN_ZA_POSILJANJE, "", "a" * 64),
        (Invoice.Status.IZBRAN_ZA_POSILJANJE, "invoice.pdf", ""),
    ],
)
def test_prepare_rejects_ineligible_or_missing_pdf(
    issued_invoice, status, path, checksum
):
    Invoice.objects.filter(pk=issued_invoice.pk).update(
        status=status, rendered_pdf_path=path, checksum=checksum
    )

    with pytest.raises(ValidationError):
        prepare_attempt(issued_invoice)

    assert not DeliveryAttempt.objects.exists()


def test_accepted_transition_marks_invoice_sent_and_blocks_resend(issued_invoice):
    attempt = mark_submission_started(prepare_attempt(issued_invoice))
    attempt = mark_accepted(attempt, code="SMTP_ACCEPTED", message="Accepted")

    issued_invoice.refresh_from_db()
    assert attempt.result == DeliveryAttempt.Result.ACCEPTED
    assert issued_invoice.status == Invoice.Status.POSLAN
    with pytest.raises(ValidationError):
        prepare_attempt(issued_invoice)


def test_confirmed_failure_is_retry_eligible_and_preserves_issue_data(
    issued_invoice,
):
    original = (
        issued_invoice.stevilka,
        issued_invoice.rendered_pdf_path,
        issued_invoice.checksum,
        list(InvoiceLine.objects.filter(invoice=issued_invoice).values()),
        NumberSequence.objects.get(company=issued_invoice.company, tip="NP").zadnja_stevilka,
    )
    attempt = prepare_attempt(issued_invoice)
    failed = mark_confirmed_failure(
        attempt,
        code="CONNECTION_FAILED",
        message="password=hunter2 connection failed",
    )

    issued_invoice.refresh_from_db()
    current = (
        issued_invoice.stevilka,
        issued_invoice.rendered_pdf_path,
        issued_invoice.checksum,
        list(InvoiceLine.objects.filter(invoice=issued_invoice).values()),
        NumberSequence.objects.get(company=issued_invoice.company, tip="NP").zadnja_stevilka,
    )
    assert failed.result == DeliveryAttempt.Result.CONFIRMED_FAILURE
    assert issued_invoice.status == Invoice.Status.NAPAKA
    assert is_retry_eligible(issued_invoice)
    assert failed.message == "password=[REDACTED] connection failed"
    assert current == original
    assert DeliveryAttempt.objects.get(pk=attempt.pk).result == failed.result


def test_prepare_retry_creates_prepared_attempt_and_reopens_invoice(issued_invoice):
    failed = mark_confirmed_failure(
        prepare_attempt(issued_invoice), code="CONNECTION_FAILED"
    )
    failed_before = DeliveryAttempt.objects.values().get(pk=failed.pk)

    retry = prepare_retry_attempt(failed)

    issued_invoice.refresh_from_db()
    assert (retry.sequence, retry.result) == (2, DeliveryAttempt.Result.PREPARED)
    assert retry.prepared_at > failed.prepared_at
    assert retry.submission_started_at is None
    assert retry.completed_at is None
    assert (retry.result_code, retry.message) == ("", "")
    assert issued_invoice.status == Invoice.Status.IZBRAN_ZA_POSILJANJE
    assert DeliveryAttempt.objects.values().get(pk=failed.pk) == failed_before


def test_prepare_retry_clones_failed_snapshot_not_live_contacts(issued_invoice):
    failed = mark_confirmed_failure(
        prepare_attempt(issued_invoice), code="CONNECTION_FAILED"
    )
    Company.objects.filter(pk=issued_invoice.company_id).update(
        email_posiljatelj="new-sender@example.com",
        email_racunovodja="new-accountant@example.com",
    )
    RecipientContact.objects.filter(lease=issued_invoice.lease).delete()
    RecipientContact.objects.create(
        lease=issued_invoice.lease,
        email="new-to@example.com",
        vloga=RecipientContact.Vloga.TO,
        kanal=RecipientContact.Kanal.PDF,
    )

    retry = prepare_retry_attempt(failed)

    assert (
        retry.sender,
        retry.recipients_to,
        retry.recipients_cc,
        retry.recipients_bcc,
        retry.pdf_path,
        retry.pdf_checksum,
    ) == (
        failed.sender,
        failed.recipients_to,
        failed.recipients_cc,
        failed.recipients_bcc,
        failed.pdf_path,
        failed.pdf_checksum,
    )


def test_prepare_retry_preserves_issued_document(issued_invoice):
    failed = mark_confirmed_failure(
        prepare_attempt(issued_invoice), code="CONNECTION_FAILED"
    )
    before = (
        Invoice.objects.values().get(pk=issued_invoice.pk),
        list(InvoiceLine.objects.filter(invoice=issued_invoice).values()),
        list(NumberSequence.objects.filter(company=issued_invoice.company).values()),
        Invoice.objects.count(),
    )

    prepare_retry_attempt(failed)

    invoice_before = before[0]
    invoice_before["status"] = Invoice.Status.IZBRAN_ZA_POSILJANJE
    assert (
        Invoice.objects.values().get(pk=issued_invoice.pk),
        list(InvoiceLine.objects.filter(invoice=issued_invoice).values()),
        list(NumberSequence.objects.filter(company=issued_invoice.company).values()),
        Invoice.objects.count(),
    ) == before


def test_prepare_retry_rejects_duplicate_from_stale_failure(issued_invoice):
    failed = mark_confirmed_failure(
        prepare_attempt(issued_invoice), code="CONNECTION_FAILED"
    )
    prepare_retry_attempt(failed)

    with pytest.raises(ValidationError):
        prepare_retry_attempt(failed)

    assert list(
        DeliveryAttempt.objects.filter(invoice=issued_invoice).values_list(
            "sequence", flat=True
        )
    ) == [1, 2]


def test_prepare_retry_rejects_confirmed_failure_that_is_not_latest(issued_invoice):
    failed = mark_confirmed_failure(
        prepare_attempt(issued_invoice), code="CONNECTION_FAILED"
    )
    DeliveryAttempt.objects.create(
        invoice=issued_invoice,
        sequence=2,
        sender=failed.sender,
        recipients_to=failed.recipients_to,
        recipients_cc=failed.recipients_cc,
        recipients_bcc=failed.recipients_bcc,
        pdf_path=failed.pdf_path,
        pdf_checksum=failed.pdf_checksum,
    )

    with pytest.raises(ValidationError):
        prepare_retry_attempt(failed)

    issued_invoice.refresh_from_db()
    assert issued_invoice.status == Invoice.Status.NAPAKA
    assert DeliveryAttempt.objects.filter(invoice=issued_invoice).count() == 2


@pytest.mark.parametrize(
    "result",
    [
        DeliveryAttempt.Result.PREPARED,
        DeliveryAttempt.Result.SUBMISSION_STARTED,
        DeliveryAttempt.Result.ACCEPTED,
        DeliveryAttempt.Result.UNKNOWN,
    ],
)
def test_prepare_retry_rejects_nonfailure_states(issued_invoice, result):
    attempt = prepare_attempt(issued_invoice)
    DeliveryAttempt.objects.filter(pk=attempt.pk).update(result=result)
    Invoice.objects.filter(pk=issued_invoice.pk).update(status=Invoice.Status.NAPAKA)

    with pytest.raises(ValidationError):
        prepare_retry_attempt(attempt)

    assert DeliveryAttempt.objects.filter(invoice=issued_invoice).count() == 1
    assert Invoice.objects.get(pk=issued_invoice.pk).status == Invoice.Status.NAPAKA


def test_prepare_retry_rejects_wrong_invoice_status(issued_invoice):
    failed = mark_confirmed_failure(
        prepare_attempt(issued_invoice), code="CONNECTION_FAILED"
    )
    Invoice.objects.filter(pk=issued_invoice.pk).update(status=Invoice.Status.POSLAN)

    with pytest.raises(ValidationError):
        prepare_retry_attempt(failed)

    assert DeliveryAttempt.objects.filter(invoice=issued_invoice).count() == 1
    assert Invoice.objects.get(pk=issued_invoice.pk).status == Invoice.Status.POSLAN


def test_prepare_retry_rolls_back_attempt_if_invoice_reopen_fails(
    issued_invoice, monkeypatch
):
    failed = mark_confirmed_failure(
        prepare_attempt(issued_invoice), code="CONNECTION_FAILED"
    )
    monkeypatch.setattr("delivery.services.Invoice.objects.filter", lambda **kwargs: None)

    with pytest.raises(AttributeError):
        prepare_retry_attempt(failed)

    assert DeliveryAttempt.objects.filter(invoice=issued_invoice).count() == 1
    assert Invoice.objects.get(pk=issued_invoice.pk).status == Invoice.Status.NAPAKA


@pytest.mark.parametrize(
    "values",
    [
        {"rendered_pdf_path": "other.pdf"},
        {"checksum": "b" * 64},
        {"checksum": "invalid"},
        {"stevilka": ""},
    ],
)
def test_prepare_retry_rejects_changed_or_invalid_document(issued_invoice, values):
    failed = mark_confirmed_failure(
        prepare_attempt(issued_invoice), code="CONNECTION_FAILED"
    )
    Invoice.objects.filter(pk=issued_invoice.pk).update(**values)

    with pytest.raises(ValidationError):
        prepare_retry_attempt(failed)

    assert DeliveryAttempt.objects.filter(invoice=issued_invoice).count() == 1
    assert Invoice.objects.get(pk=issued_invoice.pk).status == Invoice.Status.NAPAKA


def test_prepare_retry_is_db_only(issued_invoice, monkeypatch):
    failed = mark_confirmed_failure(
        prepare_attempt(issued_invoice), code="CONNECTION_FAILED"
    )
    monkeypatch.setattr(
        "delivery.services._recipient_snapshots",
        lambda invoice: pytest.fail("live recipients read"),
    )
    monkeypatch.setattr(
        "rendering.services.stanje_koncnega_pdf",
        lambda invoice: pytest.fail("filesystem read"),
    )
    monkeypatch.setattr(
        "delivery.credentials.load_password",
        lambda ref: pytest.fail("keyring read"),
    )
    monkeypatch.setattr(
        "delivery.transport.send_prepared_email",
        lambda *args, **kwargs: pytest.fail("SMTP opened"),
    )

    assert prepare_retry_attempt(failed).result == DeliveryAttempt.Result.PREPARED


def test_retry_composes_same_verified_pdf_and_envelope(prepared_email_data):
    _, attempt, _, pdf_bytes = prepared_email_data
    failed = mark_confirmed_failure(attempt, code="CONNECTION_FAILED")
    retry = prepare_retry_attempt(failed)

    prepared = build_pdf_email(retry, subject="Invoice", body="Attached invoice.")

    assert prepared.envelope_sender == failed.sender
    assert prepared.envelope_recipients == tuple(
        failed.recipients_to + failed.recipients_cc + failed.recipients_bcc
    )
    assert next(prepared.message.iter_attachments()).get_payload(decode=True) == pdf_bytes


@pytest.mark.parametrize(
    ("message", "secret", "expected"),
    [
        (
            "SMTP rejected Authorization: Bearer abc123 during login",
            "abc123",
            "SMTP rejected Authorization: [REDACTED] during login",
        ),
        (
            'Login failed password="hello world" after handshake',
            "hello world",
            "Login failed password=[REDACTED] after handshake",
        ),
        (
            "Login failed token=abc123 after handshake",
            "abc123",
            "Login failed token=[REDACTED] after handshake",
        ),
        (
            "Login failed Bearer abc123 after handshake",
            "abc123",
            "Login failed Bearer [REDACTED] after handshake",
        ),
    ],
)
def test_persisted_message_redacts_common_credentials(
    issued_invoice, message, secret, expected
):
    failed = mark_confirmed_failure(
        prepare_attempt(issued_invoice), code="AUTH_FAILED", message=message
    )

    persisted = DeliveryAttempt.objects.get(pk=failed.pk).message
    assert secret not in persisted
    assert persisted == expected


def test_confirmed_failure_after_submission_marks_invoice_failed(issued_invoice):
    attempt = mark_submission_started(prepare_attempt(issued_invoice))

    failed = mark_confirmed_failure(
        attempt, code="SMTP_REJECTED", message="550 Message rejected"
    )

    issued_invoice.refresh_from_db()
    assert failed.result == DeliveryAttempt.Result.CONFIRMED_FAILURE
    assert failed.result_code == "SMTP_REJECTED"
    assert failed.message == "550 Message rejected"
    assert failed.completed_at is not None
    assert issued_invoice.status == Invoice.Status.NAPAKA
    assert is_retry_eligible(issued_invoice)


def test_stale_prepared_cannot_fail_started_attempt(issued_invoice):
    stale = prepare_attempt(issued_invoice)
    mark_submission_started(stale)

    with pytest.raises(ValidationError):
        mark_confirmed_failure(stale, code="SMTP_CONNECT_FAILED")

    persisted = DeliveryAttempt.objects.get(pk=stale.pk)
    issued_invoice.refresh_from_db()
    assert persisted.result == DeliveryAttempt.Result.SUBMISSION_STARTED
    assert issued_invoice.status == Invoice.Status.IZBRAN_ZA_POSILJANJE


@pytest.mark.parametrize(
    "terminal_result",
    [
        DeliveryAttempt.Result.ACCEPTED,
        DeliveryAttempt.Result.UNKNOWN,
        DeliveryAttempt.Result.CONFIRMED_FAILURE,
    ],
)
def test_confirmed_failure_rejects_terminal_attempt(issued_invoice, terminal_result):
    attempt = prepare_attempt(issued_invoice)
    DeliveryAttempt.objects.filter(pk=attempt.pk).update(result=terminal_result)

    with pytest.raises(ValidationError):
        mark_confirmed_failure(attempt, code="SMTP_REJECTED")

    issued_invoice.refresh_from_db()
    assert issued_invoice.status == Invoice.Status.IZBRAN_ZA_POSILJANJE


def test_unknown_after_submission_blocks_retry(issued_invoice):
    attempt = mark_submission_started(prepare_attempt(issued_invoice))
    unknown = mark_result_unknown(
        attempt, code="CONNECTION_LOST", message="No final response"
    )

    issued_invoice.refresh_from_db()
    assert unknown.result == DeliveryAttempt.Result.UNKNOWN
    assert issued_invoice.status == Invoice.Status.REZULTAT_NI_ZNAN
    assert not is_retry_eligible(issued_invoice)
    with pytest.raises(ValidationError):
        prepare_attempt(issued_invoice)


def _unknown_attempt(invoice):
    return mark_result_unknown(
        mark_submission_started(prepare_attempt(invoice)),
        code="SMTP_RESULT_UNKNOWN",
        message="No final response",
    )


def _resolution_audits(attempt):
    return AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(DeliveryAttempt),
        object_id=attempt.pk,
        polje=UNKNOWN_RESOLUTION_FIELD,
    )


def test_resolve_unknown_as_sent_audits_without_rewriting_attempt(issued_invoice):
    unknown = _unknown_attempt(issued_invoice)
    before = DeliveryAttempt.objects.values().get(pk=unknown.pk)

    resolved = resolve_unknown_as_sent(unknown, reason="Verified in Sent folder")

    issued_invoice.refresh_from_db()
    audit = _resolution_audits(unknown).get()
    assert resolved.pk == unknown.pk
    assert issued_invoice.status == Invoice.Status.POSLAN
    assert DeliveryAttempt.objects.values().get(pk=unknown.pk) == before
    assert DeliveryAttempt.objects.count() == 1
    assert (
        audit.dejanje,
        audit.polje,
        audit.stara_vrednost,
        audit.nova_vrednost,
        audit.razlog,
    ) == (
        AuditLogEntry.Dejanje.SPREMEMBA,
        "unknown_resolution",
        UNKNOWN_RESOLUTION_UNRESOLVED,
        UNKNOWN_RESOLUTION_SENT_VERIFIED,
        "Verified in Sent folder",
    )


@pytest.mark.parametrize("reason", ["", "   ", None])
@pytest.mark.parametrize(
    "service",
    [resolve_unknown_as_sent, resolve_unknown_user_confirmed_not_sent_and_prepare_retry],
)
def test_unknown_resolution_requires_reason(issued_invoice, reason, service):
    unknown = _unknown_attempt(issued_invoice)

    with pytest.raises(ValidationError):
        service(unknown, reason=reason)

    assert not _resolution_audits(unknown).exists()
    assert Invoice.objects.get(pk=issued_invoice.pk).status == Invoice.Status.REZULTAT_NI_ZNAN


@pytest.mark.parametrize(
    "result",
    [
        DeliveryAttempt.Result.PREPARED,
        DeliveryAttempt.Result.SUBMISSION_STARTED,
        DeliveryAttempt.Result.ACCEPTED,
        DeliveryAttempt.Result.CONFIRMED_FAILURE,
    ],
)
@pytest.mark.parametrize(
    "service",
    [resolve_unknown_as_sent, resolve_unknown_user_confirmed_not_sent_and_prepare_retry],
)
def test_unknown_resolution_rejects_other_results(issued_invoice, result, service):
    attempt = prepare_attempt(issued_invoice)
    DeliveryAttempt.objects.filter(pk=attempt.pk).update(result=result)
    Invoice.objects.filter(pk=issued_invoice.pk).update(
        status=Invoice.Status.REZULTAT_NI_ZNAN
    )

    with pytest.raises(ValidationError):
        service(attempt, reason="Decision")

    assert not AuditLogEntry.objects.exists()
    assert DeliveryAttempt.objects.count() == 1


@pytest.mark.parametrize(
    "service",
    [resolve_unknown_as_sent, resolve_unknown_user_confirmed_not_sent_and_prepare_retry],
)
def test_unknown_resolution_rejects_unsaved_or_missing_attempt(issued_invoice, service):
    for attempt in (DeliveryAttempt(), DeliveryAttempt(pk=999999)):
        with pytest.raises(ValidationError):
            service(attempt, reason="Decision")

    assert not AuditLogEntry.objects.exists()
    assert not DeliveryAttempt.objects.exists()


def test_explicit_not_sent_creates_snapshot_retry_and_audit(issued_invoice):
    unknown = _unknown_attempt(issued_invoice)
    source_before = DeliveryAttempt.objects.values().get(pk=unknown.pk)
    Company.objects.filter(pk=issued_invoice.company_id).update(
        email_posiljatelj="new-sender@example.com",
        email_racunovodja="new-accountant@example.com",
    )
    update_business_records(RecipientContact.objects.filter(lease=issued_invoice.lease),
        email="new-contact@example.com"
    )

    retry = resolve_unknown_user_confirmed_not_sent_and_prepare_retry(
        unknown, reason="User checked and confirmed not sent"
    )

    issued_invoice.refresh_from_db()
    audit = _resolution_audits(unknown).get()
    assert (retry.sequence, retry.result) == (2, DeliveryAttempt.Result.PREPARED)
    assert issued_invoice.status == Invoice.Status.IZBRAN_ZA_POSILJANJE
    assert DeliveryAttempt.objects.values().get(pk=unknown.pk) == source_before
    assert (
        retry.sender,
        retry.recipients_to,
        retry.recipients_cc,
        retry.recipients_bcc,
        retry.pdf_path,
        retry.pdf_checksum,
    ) == (
        unknown.sender,
        unknown.recipients_to,
        unknown.recipients_cc,
        unknown.recipients_bcc,
        unknown.pdf_path,
        unknown.pdf_checksum,
    )
    assert retry.submission_started_at is None
    assert retry.completed_at is None
    assert (retry.result_code, retry.message) == ("", "")
    assert (audit.stara_vrednost, audit.nova_vrednost, audit.razlog) == (
        UNKNOWN_RESOLUTION_UNRESOLVED,
        UNKNOWN_RESOLUTION_NOT_SENT_CONFIRMED,
        "User checked and confirmed not sent",
    )


def test_unresolved_unknown_cannot_use_confirmed_failure_retry(issued_invoice):
    unknown = _unknown_attempt(issued_invoice)

    with pytest.raises(ValidationError):
        prepare_retry_attempt(unknown)

    assert DeliveryAttempt.objects.count() == 1
    assert Invoice.objects.get(pk=issued_invoice.pk).status == Invoice.Status.REZULTAT_NI_ZNAN


@pytest.mark.parametrize("resolution", ["sent", "not_sent"])
def test_unknown_resolution_rejects_duplicate_or_stale_attempt(issued_invoice, resolution):
    unknown = _unknown_attempt(issued_invoice)
    service = (
        resolve_unknown_as_sent
        if resolution == "sent"
        else resolve_unknown_user_confirmed_not_sent_and_prepare_retry
    )
    service(unknown, reason="First decision")

    with pytest.raises(ValidationError):
        service(unknown, reason="Duplicate decision")

    assert _resolution_audits(unknown).count() == 1
    assert DeliveryAttempt.objects.count() == (1 if resolution == "sent" else 2)


@pytest.mark.parametrize(
    "service",
    [resolve_unknown_as_sent, resolve_unknown_user_confirmed_not_sent_and_prepare_retry],
)
def test_unknown_resolution_rejects_nonlatest_and_wrong_invoice_state(
    issued_invoice, service
):
    unknown = _unknown_attempt(issued_invoice)
    DeliveryAttempt.objects.create(
        invoice=issued_invoice,
        sequence=2,
        sender=unknown.sender,
        recipients_to=unknown.recipients_to,
        recipients_cc=unknown.recipients_cc,
        recipients_bcc=unknown.recipients_bcc,
        pdf_path=unknown.pdf_path,
        pdf_checksum=unknown.pdf_checksum,
    )

    with pytest.raises(ValidationError):
        service(unknown, reason="Decision")

    assert not _resolution_audits(unknown).exists()
    assert Invoice.objects.get(pk=issued_invoice.pk).status == Invoice.Status.REZULTAT_NI_ZNAN

    DeliveryAttempt.objects.filter(sequence=2).update(result=DeliveryAttempt.Result.UNKNOWN)
    Invoice.objects.filter(pk=issued_invoice.pk).update(status=Invoice.Status.NAPAKA)
    latest = DeliveryAttempt.objects.get(sequence=2)
    with pytest.raises(ValidationError):
        service(latest, reason="Decision")
    assert not _resolution_audits(latest).exists()


@pytest.mark.parametrize(
    "values",
    [
        {"rendered_pdf_path": "other.pdf"},
        {"checksum": "b" * 64},
        {"stevilka": ""},
    ],
)
def test_unknown_not_sent_rejects_changed_document(issued_invoice, values):
    unknown = _unknown_attempt(issued_invoice)
    Invoice.objects.filter(pk=issued_invoice.pk).update(**values)

    with pytest.raises(ValidationError):
        resolve_unknown_user_confirmed_not_sent_and_prepare_retry(
            unknown, reason="Not sent"
        )

    assert DeliveryAttempt.objects.count() == 1
    assert not _resolution_audits(unknown).exists()


def test_unknown_not_sent_rejects_invalid_source_checksum(issued_invoice):
    unknown = _unknown_attempt(issued_invoice)
    DeliveryAttempt.objects.filter(pk=unknown.pk).update(pdf_checksum="invalid")
    Invoice.objects.filter(pk=issued_invoice.pk).update(checksum="invalid")

    with pytest.raises(ValidationError):
        resolve_unknown_user_confirmed_not_sent_and_prepare_retry(
            unknown, reason="Not sent"
        )

    assert DeliveryAttempt.objects.count() == 1
    assert not _resolution_audits(unknown).exists()


def test_unknown_sent_resolution_rolls_back_audit_when_status_update_fails(
    issued_invoice, monkeypatch
):
    unknown = _unknown_attempt(issued_invoice)
    original_update = type(Invoice.objects.all()).update

    def fail_update(queryset, **kwargs):
        if kwargs.get("status") == Invoice.Status.POSLAN:
            raise RuntimeError("status write failed")
        return original_update(queryset, **kwargs)

    monkeypatch.setattr(type(Invoice.objects.all()), "update", fail_update)
    with pytest.raises(RuntimeError, match="status write failed"):
        resolve_unknown_as_sent(unknown, reason="Verified")

    assert not _resolution_audits(unknown).exists()
    assert Invoice.objects.get(pk=issued_invoice.pk).status == Invoice.Status.REZULTAT_NI_ZNAN


def test_unknown_not_sent_rolls_back_audit_and_retry_when_status_update_fails(
    issued_invoice, monkeypatch
):
    unknown = _unknown_attempt(issued_invoice)
    original_update = type(Invoice.objects.all()).update

    def fail_update(queryset, **kwargs):
        if kwargs.get("status") == Invoice.Status.IZBRAN_ZA_POSILJANJE:
            raise RuntimeError("status write failed")
        return original_update(queryset, **kwargs)

    monkeypatch.setattr(type(Invoice.objects.all()), "update", fail_update)
    with pytest.raises(RuntimeError, match="status write failed"):
        resolve_unknown_user_confirmed_not_sent_and_prepare_retry(
            unknown, reason="Not sent"
        )

    assert not _resolution_audits(unknown).exists()
    assert DeliveryAttempt.objects.count() == 1
    assert Invoice.objects.get(pk=issued_invoice.pk).status == Invoice.Status.REZULTAT_NI_ZNAN


@pytest.mark.parametrize(
    "service",
    [resolve_unknown_as_sent, resolve_unknown_user_confirmed_not_sent_and_prepare_retry],
)
def test_unknown_resolution_is_db_only(issued_invoice, monkeypatch, service):
    unknown = _unknown_attempt(issued_invoice)
    for target in (
        "delivery.services._recipient_snapshots",
        "rendering.services.stanje_koncnega_pdf",
        "delivery.credentials.load_password",
        "delivery.transport.send_prepared_email",
    ):
        monkeypatch.setattr(target, lambda *args, **kwargs: pytest.fail("external I/O"))

    resolved = service(unknown, reason="Resolution evidence")
    expected = (
        DeliveryAttempt.Result.UNKNOWN
        if service is resolve_unknown_as_sent
        else DeliveryAttempt.Result.PREPARED
    )
    assert resolved.result == expected


def test_invalid_transition_order_and_audit_mutation_are_rejected(issued_invoice):
    attempt = prepare_attempt(issued_invoice)

    with pytest.raises(ValidationError):
        mark_accepted(attempt, code="SMTP_ACCEPTED")
    attempt.message = "overwrite"
    with pytest.raises(ValidationError):
        attempt.save()
    with pytest.raises(ValidationError):
        attempt.delete()

    assert DeliveryAttempt.objects.filter(pk=attempt.pk).exists()


def test_build_pdf_email_uses_snapshots_envelope_and_verified_attachment(
    prepared_email_data,
):
    invoice, attempt, _, pdf_bytes = prepared_email_data
    update_business_records(RecipientContact.objects.filter(lease=invoice.lease),
        email="changed@example.com", aktiven=False
    )
    Company.objects.filter(pk=invoice.company_id).update(
        email_posiljatelj="changed-sender@example.com"
    )

    prepared = build_pdf_email(attempt, subject="Račun", body="V prilogi je račun.")
    attachment = next(prepared.message.iter_attachments())

    assert prepared.envelope_sender == "sender@example.com"
    assert prepared.message["From"] == "sender@example.com"
    assert prepared.message["To"] == "to@example.com"
    assert prepared.message["Cc"] == "cc@example.com"
    assert prepared.message["Bcc"] is None
    assert prepared.envelope_recipients == (
        "to@example.com",
        "cc@example.com",
        "accountant@example.com",
    )
    assert attachment.get_payload(decode=True) == pdf_bytes
    assert attachment.get_content_type() == "application/pdf"
    assert attachment.get_filename() == f"{invoice.stevilka}.pdf"
    assert len(tuple(prepared.message.iter_attachments())) == 1
    assert (prepared.invoice_id, prepared.attempt_id) == (invoice.pk, attempt.pk)


def test_envelope_deduplicates_case_insensitively_without_changing_headers(
    prepared_email_data,
):
    _, attempt, _, _ = prepared_email_data
    DeliveryAttempt.objects.filter(pk=attempt.pk).update(
        recipients_to=["First@Example.com", "to@example.com"],
        recipients_cc=["first@example.com", "CC@example.com"],
        recipients_bcc=["cc@EXAMPLE.com", "accountant@example.com"],
    )

    prepared = build_pdf_email(attempt, subject="Račun", body="Račun")

    assert prepared.message["To"] == "First@Example.com, to@example.com"
    assert prepared.message["Cc"] == "first@example.com, CC@example.com"
    assert prepared.message["Bcc"] is None
    assert prepared.envelope_recipients == (
        "First@Example.com",
        "to@example.com",
        "CC@example.com",
        "accountant@example.com",
    )


@pytest.mark.parametrize("value", ["", "   ", None, 42])
@pytest.mark.parametrize("field", ["subject", "body"])
def test_build_pdf_email_requires_nonempty_text(prepared_email_data, field, value):
    _, attempt, _, _ = prepared_email_data
    arguments = {"subject": "Račun", "body": "Račun"}
    arguments[field] = value

    with pytest.raises(ValidationError):
        build_pdf_email(attempt, **arguments)


def test_build_pdf_email_rejects_header_injection_and_unsaved_attempt(
    prepared_email_data,
):
    _, attempt, _, _ = prepared_email_data
    with pytest.raises(ValidationError):
        build_pdf_email(attempt, subject="Račun\nBcc: leaked@example.com", body="Račun")

    attempt.pk = None
    with pytest.raises(ValidationError):
        build_pdf_email(attempt, subject="Račun", body="Račun")


@pytest.mark.parametrize("kind", ["missing", "corrupt"])
def test_missing_or_corrupt_pdf_blocks_composition(prepared_email_data, kind):
    _, attempt, artifact, _ = prepared_email_data
    if kind == "missing":
        artifact.unlink()
    else:
        artifact.write_bytes(b"corrupt")

    with pytest.raises(ValidationError):
        build_pdf_email(attempt, subject="Račun", body="Račun")


@pytest.mark.parametrize("target", ["invoice", "attempt"])
def test_pdf_metadata_mismatch_blocks_composition(prepared_email_data, target):
    invoice, attempt, _, _ = prepared_email_data
    if target == "invoice":
        Invoice.objects.filter(pk=invoice.pk).update(checksum="b" * 64)
    else:
        DeliveryAttempt.objects.filter(pk=attempt.pk).update(pdf_checksum="b" * 64)

    with pytest.raises(ValidationError):
        build_pdf_email(attempt, subject="Račun", body="Račun")


def test_nonprepared_attempt_and_eslog_metadata_are_rejected(prepared_email_data):
    invoice, attempt, _, _ = prepared_email_data
    DeliveryAttempt.objects.filter(pk=attempt.pk).update(
        result=DeliveryAttempt.Result.SUBMISSION_STARTED
    )
    with pytest.raises(ValidationError):
        build_pdf_email(attempt, subject="Račun", body="Račun")

    DeliveryAttempt.objects.filter(pk=attempt.pk).update(
        result=DeliveryAttempt.Result.PREPARED
    )
    Invoice.objects.filter(pk=invoice.pk).update(rendered_eslog_path="unexpected.xml")
    with pytest.raises(ValidationError):
        build_pdf_email(attempt, subject="Račun", body="Račun")


@pytest.mark.parametrize(
    "field,value",
    [
        ("sender", "invalid"),
        ("recipients_to", []),
        ("recipients_to", ["invalid"]),
        ("recipients_cc", ["invalid"]),
        ("recipients_bcc", ["invalid"]),
    ],
)
def test_invalid_sender_or_envelope_recipient_is_rejected(
    prepared_email_data, field, value
):
    _, attempt, _, _ = prepared_email_data
    DeliveryAttempt.objects.filter(pk=attempt.pk).update(**{field: value})

    with pytest.raises(ValidationError):
        build_pdf_email(attempt, subject="Račun", body="Račun")


@pytest.mark.django_db(transaction=True)
def test_integrity_check_runs_outside_transaction(prepared_email_data, monkeypatch):
    _, attempt, _, _ = prepared_email_data
    import rendering.services

    original = rendering.services.stanje_koncnega_pdf

    def observed(invoice):
        assert connection.in_atomic_block is False
        return original(invoice)

    monkeypatch.setattr(rendering.services, "stanje_koncnega_pdf", observed)
    build_pdf_email(attempt, subject="Račun", body="Račun")


def test_composition_does_not_mutate_business_state(prepared_email_data):
    invoice, attempt, _, _ = prepared_email_data
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2026, 8, 1), datum_izdaje=date(2026, 9, 1)
    )
    before = (
        Invoice.objects.values().get(pk=invoice.pk),
        DeliveryAttempt.objects.values().get(pk=attempt.pk),
        list(InvoiceLine.objects.filter(invoice=invoice).values()),
        list(NumberSequence.objects.filter(company=invoice.company).values()),
        MonthlyCycle.objects.values().get(pk=cycle.pk),
    )

    build_pdf_email(attempt, subject="Račun", body="Račun")

    after = (
        Invoice.objects.values().get(pk=invoice.pk),
        DeliveryAttempt.objects.values().get(pk=attempt.pk),
        list(InvoiceLine.objects.filter(invoice=invoice).values()),
        list(NumberSequence.objects.filter(company=invoice.company).values()),
        MonthlyCycle.objects.values().get(pk=cycle.pk),
    )
    assert after == before


def _send(attempt, smtp_config, fake, loader=password_loader):
    return send_pdf_email(
        attempt,
        subject="Invoice",
        body="Attached invoice.",
        smtp_config=smtp_config,
        credential_ref=KeyringCredentialRef("marjan", "smtp-user"),
        password_loader=loader,
        smtp_factory=smtp_factory(fake),
    )


def test_send_accepts_message_and_all_recipients(prepared_email_data, smtp_config):
    invoice, attempt, _, pdf_bytes = prepared_email_data

    def data_result(payload):
        persisted = DeliveryAttempt.objects.get(pk=attempt.pk)
        assert persisted.result == DeliveryAttempt.Result.SUBMISSION_STARTED
        assert connection.in_atomic_block is False
        return 250, b"queued"

    fake = FakeSmtp({"data": data_result})
    before = DeliveryAttempt.objects.values().get(pk=attempt.pk)
    result = _send(attempt, smtp_config, fake)
    invoice.refresh_from_db()
    assert (result.result, invoice.status) == (
        DeliveryAttempt.Result.ACCEPTED,
        Invoice.Status.POSLAN,
    )
    assert [call[1] for call in fake.calls if call[0] == "rcpt_call"] == [
        "to@example.com",
        "cc@example.com",
        "accountant@example.com",
    ]
    payload = next(call[1] for call in fake.calls if call[0] == "data")
    assert b"\n" not in payload.replace(b"\r\n", b"")
    assert b"Bcc:" not in payload
    assert b"Content-Type: application/pdf" in payload
    parsed = BytesParser(policy=policy.default).parsebytes(payload)
    assert parsed.get_payload()[-1].get_payload(decode=True) == pdf_bytes
    for field in (
        "sender",
        "recipients_to",
        "recipients_cc",
        "recipients_bcc",
        "pdf_path",
        "pdf_checksum",
    ):
        assert getattr(result, field) == before[field]


@pytest.mark.parametrize("code", ["KEYCHAIN_CREDENTIAL_MISSING", "KEYCHAIN_ERROR"])
def test_keychain_failure_is_confirmed_without_network(
    prepared_email_data, smtp_config, code
):
    invoice, attempt, _, _ = prepared_email_data

    def loader(ref):
        assert connection.in_atomic_block is False
        raise CredentialError(code, "Safe credential error.")

    result = send_pdf_email(
        attempt,
        subject="Invoice",
        body="Attached.",
        smtp_config=smtp_config,
        credential_ref=KeyringCredentialRef("marjan", "smtp-user"),
        password_loader=loader,
        smtp_factory=lambda config, context: pytest.fail("network opened"),
    )
    invoice.refresh_from_db()
    assert (result.result, result.result_code, invoice.status) == (
        DeliveryAttempt.Result.CONFIRMED_FAILURE,
        code,
        Invoice.Status.NAPAKA,
    )
    assert "top-secret" not in repr(result)


@pytest.mark.parametrize(
    ("behavior", "expected_code"),
    [
        (
            {"login": __import__("smtplib").SMTPAuthenticationError(535, b"denied")},
            "SMTP_AUTH_FAILED",
        ),
        ({"ehlo": TimeoutError("late")}, "SMTP_TIMEOUT"),
        (
            {"ehlo": __import__("smtplib").SMTPServerDisconnected("gone")},
            "SMTP_CONNECTION_LOST",
        ),
    ],
)
def test_setup_failures_are_distinct(
    prepared_email_data, smtp_config, behavior, expected_code
):
    invoice, attempt, _, _ = prepared_email_data
    result = _send(attempt, smtp_config, FakeSmtp(behavior))
    invoice.refresh_from_db()
    assert (result.result, result.result_code, invoice.status) == (
        DeliveryAttempt.Result.CONFIRMED_FAILURE,
        expected_code,
        Invoice.Status.NAPAKA,
    )


def test_connect_failure_is_safe_and_confirmed(prepared_email_data, smtp_config):
    _, attempt, _, _ = prepared_email_data

    def fail_connect(config, context):
        assert connection.in_atomic_block is False
        raise ConnectionRefusedError("unreachable top-secret")

    result = send_pdf_email(
        attempt,
        subject="Invoice",
        body="Attached.",
        smtp_config=smtp_config,
        credential_ref=KeyringCredentialRef("marjan", "smtp-user"),
        password_loader=password_loader,
        smtp_factory=fail_connect,
    )
    assert result.result_code == "SMTP_CONNECT_FAILED"
    assert "top-secret" not in result.message


@pytest.mark.parametrize(
    ("behavior", "expected_code", "message"),
    [
        ({"mail": (550, b"sender denied")}, "SMTP_SENDER_REJECTED", ""),
        (
            {"rcpt": {"cc@example.com": (550, b"unknown mailbox")}},
            "SMTP_RECIPIENT_REJECTED",
            "cc@example.com",
        ),
    ],
)
def test_envelope_rejection_prevents_data(
    prepared_email_data, smtp_config, behavior, expected_code, message
):
    _, attempt, _, _ = prepared_email_data
    fake = FakeSmtp(behavior)
    result = _send(attempt, smtp_config, fake)
    assert result.result_code == expected_code
    assert message in result.message
    assert not any(call[0] == "data" for call in fake.calls)


def test_first_recipient_rejection_stops_later_rcpt_commands(
    prepared_email_data, smtp_config
):
    _, attempt, _, _ = prepared_email_data
    fake = FakeSmtp(
        {
            "rcpt": {
                "to@example.com": (550, b"unknown mailbox"),
                "cc@example.com": ConnectionResetError("must not be reached"),
            }
        }
    )

    result = _send(attempt, smtp_config, fake)

    assert result.result_code == "SMTP_RECIPIENT_REJECTED"
    assert result.message == "to@example.com: 550 unknown mailbox"
    assert [call[1] for call in fake.calls if call[0] == "rcpt_call"] == [
        "to@example.com"
    ]
    assert not any(call[0] == "data" for call in fake.calls)


def test_stale_send_failure_cannot_overwrite_another_started_send(
    prepared_email_data, smtp_config
):
    invoice, attempt, _, _ = prepared_email_data

    def racing_loader(ref):
        mark_submission_started(attempt)
        raise CredentialError("KEYCHAIN_ERROR", "Safe credential error.")

    with pytest.raises(ValidationError):
        send_pdf_email(
            attempt,
            subject="Invoice",
            body="Attached.",
            smtp_config=smtp_config,
            credential_ref=KeyringCredentialRef("marjan", "smtp-user"),
            password_loader=racing_loader,
            smtp_factory=lambda config, context: pytest.fail("network opened"),
        )

    persisted = DeliveryAttempt.objects.get(pk=attempt.pk)
    invoice.refresh_from_db()
    assert persisted.result == DeliveryAttempt.Result.SUBMISSION_STARTED
    assert invoice.status == Invoice.Status.IZBRAN_ZA_POSILJANJE


@pytest.mark.parametrize("response", [(450, b"temporary reject"), (550, b"reject")])
def test_explicit_data_rejection_is_confirmed(
    prepared_email_data, smtp_config, response
):
    invoice, attempt, _, _ = prepared_email_data
    result = _send(attempt, smtp_config, FakeSmtp({"data": response}))
    invoice.refresh_from_db()
    assert (result.result, result.result_code, invoice.status) == (
        DeliveryAttempt.Result.CONFIRMED_FAILURE,
        "SMTP_DATA_REJECTED",
        Invoice.Status.NAPAKA,
    )


@pytest.mark.parametrize("error", [TimeoutError("late"), ConnectionResetError("gone")])
def test_no_final_data_response_is_unknown(prepared_email_data, smtp_config, error):
    invoice, attempt, _, _ = prepared_email_data
    result = _send(attempt, smtp_config, FakeSmtp({"data": error}))
    invoice.refresh_from_db()
    assert (result.result, result.result_code, invoice.status) == (
        DeliveryAttempt.Result.UNKNOWN,
        "SMTP_RESULT_UNKNOWN",
        Invoice.Status.REZULTAT_NI_ZNAN,
    )


def test_cleanup_failure_does_not_overwrite_acceptance(
    prepared_email_data, smtp_config
):
    invoice, attempt, _, _ = prepared_email_data
    result = _send(attempt, smtp_config, FakeSmtp({"quit": OSError("gone")}))
    invoice.refresh_from_db()
    assert (result.result, invoice.status) == (
        DeliveryAttempt.Result.ACCEPTED,
        Invoice.Status.POSLAN,
    )


def test_cleanup_failure_does_not_overwrite_rejection(prepared_email_data, smtp_config):
    invoice, attempt, _, _ = prepared_email_data
    fake = FakeSmtp({"data": (550, b"rejected"), "quit": OSError("gone")})
    result = _send(attempt, smtp_config, fake)
    invoice.refresh_from_db()
    assert (result.result, invoice.status) == (
        DeliveryAttempt.Result.CONFIRMED_FAILURE,
        Invoice.Status.NAPAKA,
    )


def test_connection_test_does_not_mutate_business_state(
    prepared_email_data, smtp_config
):
    invoice, attempt, _, _ = prepared_email_data
    before = (
        Invoice.objects.values().get(pk=invoice.pk),
        DeliveryAttempt.objects.values().get(pk=attempt.pk),
    )
    result = check_smtp_connection(
        smtp_config,
        KeyringCredentialRef("marjan", "smtp-user"),
        password_loader=password_loader,
        smtp_factory=smtp_factory(FakeSmtp()),
    )
    assert result.success and result.code == "SMTP_CONNECTION_OK"
    assert before == (
        Invoice.objects.values().get(pk=invoice.pk),
        DeliveryAttempt.objects.values().get(pk=attempt.pk),
    )
    assert "top-secret" not in repr(result)


@pytest.mark.parametrize(
    ("behavior", "code"),
    [
        (
            {"login": __import__("smtplib").SMTPAuthenticationError(535, b"denied")},
            "SMTP_AUTH_FAILED",
        ),
        ({"ehlo": OSError("unreachable")}, "SMTP_CONNECT_FAILED"),
    ],
)
def test_connection_test_returns_safe_failure(smtp_config, behavior, code):
    result = check_smtp_connection(
        smtp_config,
        KeyringCredentialRef("marjan", "smtp-user"),
        password_loader=password_loader,
        smtp_factory=smtp_factory(FakeSmtp(behavior)),
    )
    assert not result.success
    assert result.code == code
    assert "top-secret" not in repr(result)


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "host": "",
            "port": 587,
            "security": SmtpSecurity.STARTTLS,
            "timeout_seconds": 1,
            "username": "u",
        },
        {
            "host": "h",
            "port": 0,
            "security": SmtpSecurity.STARTTLS,
            "timeout_seconds": 1,
            "username": "u",
        },
        {
            "host": "h",
            "port": 587,
            "security": SmtpSecurity.STARTTLS,
            "timeout_seconds": 0,
            "username": "u",
        },
        {
            "host": "h",
            "port": 587,
            "security": "STARTTLS",
            "timeout_seconds": 1,
            "username": "u",
        },
    ],
)
def test_smtp_config_is_strict(arguments):
    with pytest.raises((TypeError, ValueError)):
        SmtpConfig(**arguments)


def test_load_password_wraps_backend_error_without_secret(monkeypatch):
    fake_secret = "SUPER_SECRET_123"

    def fail(service, username):
        raise RuntimeError(f"backend failed with password {fake_secret}")

    monkeypatch.setattr("delivery.credentials.keyring.get_password", fail)
    with pytest.raises(CredentialError) as caught:
        load_password(KeyringCredentialRef("marjan", "smtp-user"))
    error = caught.value
    assert error.code == "KEYCHAIN_ERROR"
    assert str(error) == "Keychain credential lookup failed."
    assert fake_secret not in str(error)
    assert error.__cause__ is None
    assert fake_secret not in "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )


def test_composition_does_not_open_network_connection(
    prepared_email_data, monkeypatch
):
    _, attempt, _, _ = prepared_email_data

    def network_forbidden(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr("socket.socket", network_forbidden)
    build_pdf_email(attempt, subject="Račun", body="Račun")
