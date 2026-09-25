from datetime import date
from decimal import Decimal
from hashlib import sha256

import pytest
from django.core.exceptions import ValidationError
from django.db import connection

from companies.models import Company, NumberSequence
from contracts.models import Lease, Property, RecipientContact, Tenant, Unit
from delivery.models import DeliveryAttempt
from delivery.services import (
    build_pdf_email,
    is_retry_eligible,
    mark_accepted,
    mark_pre_submission_failure,
    mark_result_unknown,
    mark_submission_started,
    prepare_attempt,
)
from invoicing.models import Invoice, InvoiceLine, MonthlyCycle


@pytest.fixture
def issued_invoice(db):
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
        keychain_kljuc_posiljatelja="keychain-reference",
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


def test_pre_submission_failure_is_retry_eligible_and_preserves_issue_data(
    issued_invoice,
):
    original = (
        issued_invoice.stevilka,
        issued_invoice.rendered_pdf_path,
        issued_invoice.checksum,
        list(InvoiceLine.objects.filter(invoice=issued_invoice).values()),
        NumberSequence.objects.get(company=issued_invoice.company).zadnja_stevilka,
    )
    attempt = prepare_attempt(issued_invoice)
    failed = mark_pre_submission_failure(
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
        NumberSequence.objects.get(company=issued_invoice.company).zadnja_stevilka,
    )
    assert issued_invoice.status == Invoice.Status.NAPAKA
    assert is_retry_eligible(issued_invoice)
    assert failed.message == "password=[REDACTED] connection failed"
    assert current == original
    assert DeliveryAttempt.objects.get(pk=attempt.pk).result == failed.result


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
    failed = mark_pre_submission_failure(
        prepare_attempt(issued_invoice), code="AUTH_FAILED", message=message
    )

    persisted = DeliveryAttempt.objects.get(pk=failed.pk).message
    assert secret not in persisted
    assert persisted == expected


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
    RecipientContact.objects.filter(lease=invoice.lease).update(
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


def test_composition_does_not_open_network_connection(
    prepared_email_data, monkeypatch
):
    _, attempt, _, _ = prepared_email_data

    def network_forbidden(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr("socket.socket", network_forbidden)
    build_pdf_email(attempt, subject="Račun", body="Račun")
