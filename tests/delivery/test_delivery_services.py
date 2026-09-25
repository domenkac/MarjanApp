from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from companies.models import Company, NumberSequence
from contracts.models import Lease, Property, RecipientContact, Tenant, Unit
from delivery.models import DeliveryAttempt
from delivery.services import (
    is_retry_eligible,
    mark_accepted,
    mark_pre_submission_failure,
    mark_result_unknown,
    mark_submission_started,
    prepare_attempt,
)
from invoicing.models import Invoice, InvoiceLine


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
