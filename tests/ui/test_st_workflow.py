"""Operational ST review and issuance, using only synthetic setup and temp roots."""
# ruff: noqa: F811 -- Imported pytest fixtures are injected by name.

from contextlib import ExitStack
from hashlib import sha256
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import OperationalError, connection
from django.test import Client
from django.urls import reverse

from archive.models import ArchivedFile, BackupSnapshot
from companies.models import InitialSetupCompletion
from core.models import AuditLogEntry
from delivery.models import DeliveryAttempt
from invoicing.models import Invoice
from invoicing.services import (
    izbrisi_racun,
    spremeni_podatke_racuna,
    spremeni_vrstico,
)
from rendering.services import render_pdf
from tests.financial_fixtures import save_financial_fixture
from tests.invoicing.test_potrditev_st import (  # noqa: F401
    counters,
    draft,
    ready,
    setup_data,
)

pytestmark = pytest.mark.django_db(transaction=True)


def url(name, invoice, **kwargs):
    return reverse(f"ui:st_{name}", kwargs={
        "company_id": invoice.company_id, "invoice_id": invoice.pk, **kwargs,
    })


def reviews():
    return AuditLogEntry.objects.filter(polje="st_individual_review_v1")


def review_post(client, invoice, **changes):
    response = client.get(url("review", invoice))
    return client.post(url("record_review", invoice), {
        "fingerprint": response.context["fingerprint"], "individual_review": "on", **changes,
    })


def review_id(client, invoice):
    response = review_post(client, invoice)
    assert response.status_code == 302
    confirmation = client.get(response.url)
    assert confirmation.status_code == 200
    return confirmation.context["form"].initial["review_id"]


def issue(client, invoice, proof_id):
    return client.post(url("confirm", invoice), {"review_id": proof_id, "confirm_issue": "on"})


def test_get_only_displays_saved_snapshots_without_review_or_numbering(client, ready):
    invoice = draft(ready, opis="<script>unsafe</script>")
    before = Invoice.objects.filter(pk=invoice.pk).values().get()
    numbers, evidence = counters(), list(AuditLogEntry.objects.values())
    with patch("invoicing.st_confirmation._assets", side_effect=AssertionError("GET freezes assets")):
        response = client.get(url("review", invoice))
        assert response.status_code == 200
        content = response.content.decode()
        assert "&lt;script&gt;" in content and "<script>unsafe" not in content
        assert "Osnutek — še ni izdan" in content
        assert 'name="csrfmiddlewaretoken"' in content
        assert response.context["fingerprint"]
        assert client.get(url("record_review", invoice)).status_code == 405
        assert client.get(url("confirm", invoice)).status_code == 409
    assert Invoice.objects.filter(pk=invoice.pk).values().get() == before
    assert counters() == numbers and list(AuditLogEntry.objects.values()) == evidence


@pytest.mark.parametrize("changes", [
    {"individual_review": ""}, {"fingerprint": ""}, {"fingerprint": "bad"},
    {"fingerprint": "0" * 64},
])
def test_review_requires_valid_explicit_post(client, ready, changes):
    invoice = draft(ready)
    numbers = counters()
    assert review_post(client, invoice, **changes).status_code in (400, 409)
    assert not reviews().exists() and counters() == numbers


def test_review_stale_between_display_and_post_is_rejected(client, ready):
    invoice = draft(ready)
    fingerprint = client.get(url("review", invoice)).context["fingerprint"]
    invoice.invoiceline_set.update(opis="Spremenjeno po ogledu")
    response = client.post(url("record_review", invoice), {
        "fingerprint": fingerprint, "individual_review": "on",
    })
    assert response.status_code == 409 and not reviews().exists()


def test_review_is_saved_and_final_confirmation_is_separate(client, ready):
    invoice = draft(ready)
    numbers = counters()
    proof = review_id(client, invoice)
    assert reviews().get().pk == proof
    invoice.refresh_from_db()
    assert invoice.stevilka is None and invoice.status == "OSNUTEK"
    assert counters() == numbers
    for address in (url("review", invoice), url("confirm", invoice) + f"?review_id={proof}"):
        content = client.get(address).content.decode()
        assert "Posamično pregledan — še ni izdan" in content
    content = client.get(url("confirm", invoice), {"review_id": proof}).content.decode()
    for phrase in ("uradno izdan", "trajno številko ST", "nespremenljivi", "izbrisom računa", "korekcijski postopek"):
        assert phrase in content
    assert client.post(url("confirm", invoice), {"review_id": proof}).status_code == 200
    assert counters() == numbers


@pytest.mark.parametrize("proof", ["", "bad", "-1", "9999999999999999999999999999"])
def test_confirmation_without_valid_review_fails(client, ready, proof):
    invoice = draft(ready)
    numbers = counters()
    response = client.post(url("confirm", invoice), {"review_id": proof, "confirm_issue": "on"})
    assert response.status_code == 409
    assert counters() == numbers


@pytest.mark.parametrize("change", ["line", "issuer", "recipient", "deadline", "template"])
def test_changed_business_data_blocks_review_evidence(client, ready, settings, change):
    invoice = draft(ready)
    proof = review_id(client, invoice)
    if change == "line":
        invoice.invoiceline_set.update(opis="Novo besedilo")
    elif change == "issuer":
        ready.company.naziv = "Drugi naziv"
        save_financial_fixture(ready.company)
    elif change == "recipient":
        from tests.business_fixtures import save_business_record

        ready.lease.tenant.naziv = "Drugi prejemnik"
        save_business_record(ready.lease.tenant)
    elif change == "deadline":
        ready.company.rok_placila_dni_privzeto = 30
        save_financial_fixture(ready.company)
    else:
        settings.ST_PDF_TEMPLATE_VERSION = "changed"
    numbers = counters()
    assert issue(client, invoice, proof).status_code == 409
    invoice.refresh_from_db()
    assert invoice.stevilka is None and counters() == numbers


def test_confirmation_commits_once_then_renders_and_displays_snapshots(client, ready):
    invoice = draft(ready)
    proof = review_id(client, invoice)
    before_lines = list(invoice.invoiceline_set.values())
    calls = []

    def render_after_commit(saved):
        assert not connection.in_atomic_block
        # A separate connection sees the committed number before PDF work starts.
        other = connection.copy(alias="st_pdf_observer")
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT stevilka FROM invoicing_invoice WHERE id = %s", [saved.pk])
                assert cursor.fetchone()[0] == "ST-2026-0001"
        finally:
            other.close()
        calls.append(saved.stevilka)
        return render_pdf(saved)

    with patch("ui.st_invoices.render_pdf", side_effect=render_after_commit):
        response = issue(client, invoice, proof)
    assert response.status_code == 302 and response.url == url("detail", invoice)
    invoice.refresh_from_db()
    assert calls == ["ST-2026-0001"]
    assert invoice.stevilka == "ST-2026-0001" and invoice.status == "IZBRAN_ZA_POSILJANJE"
    assert list(invoice.invoiceline_set.values()) == before_lines
    content = client.get(response.url).content.decode()
    for value in (invoice.stevilka, invoice.prejemnik_naziv, invoice.izdajatelj_naziv,
                  "Svetovanje", "100,00", "22,00", "122,00", "16. 10. 2026", "Še ni poslan",
                  "Celovitost je preverjena", "Odpri dostavo in pregled prejemnikov", "Arhiviranje ST"):
        assert value in content
    assert url("detail", invoice) in client.get(reverse("ui:st_list")).content.decode()
    numbers = counters()
    for _ in range(2):
        assert issue(client, invoice, proof).status_code == 404
    assert counters() == numbers
    assert client.get(url("review", invoice)).status_code == 404
    assert client.get(url("confirm", invoice), {"review_id": proof}).status_code == 404


@pytest.mark.parametrize("failure", [OSError("private/path"), RuntimeError("private/database")])
def test_pdf_failure_preserves_issuance_and_pdf_only_retry(client, ready, failure):
    invoice = draft(ready)
    proof = review_id(client, invoice)
    with patch("ui.st_invoices.render_pdf", side_effect=failure):
        response = issue(client, invoice, proof)
    invoice.refresh_from_db()
    assert invoice.stevilka == "ST-2026-0001"
    assert invoice.status == "IZBRAN_ZA_POSILJANJE" and not invoice.checksum
    numbers = counters()
    for address in (response.url, url("detail", invoice)):
        detail = client.get(address).content.decode()
        assert "Račun ostaja izdan" in detail and "Ponovi samo pripravo PDF" in detail
        assert "private/" not in detail
    with patch("ui.st_invoices.potrdi_osnutek_st", side_effect=AssertionError("second confirmation")):
        assert client.post(url("retry_pdf", invoice)).status_code == 302
        assert client.post(url("retry_pdf", invoice)).status_code == 302
    invoice.refresh_from_db()
    assert invoice.checksum and counters() == numbers


@pytest.mark.parametrize("damage", ["corrupt", "missing", "path"])
def test_registered_pdf_fails_closed_without_replacement(client, ready, settings, damage):
    from django.db import models

    invoice = draft(ready)
    issue(client, invoice, review_id(client, invoice))
    invoice.refresh_from_db()
    path = settings.RENDERING_ROOT / invoice.rendered_pdf_path
    original = path.read_bytes()
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"corrupt")
    else:
        models.QuerySet.update(Invoice.objects.filter(pk=invoice.pk), rendered_pdf_path="../secret.pdf")
    before, numbers = Invoice.objects.filter(pk=invoice.pk).values().get(), counters()
    assert client.get(url("pdf", invoice)).status_code == 409
    assert "Prenos je blokiran" in client.get(url("detail", invoice)).content.decode()
    with patch("rendering.services._generiraj_pdf_bytes", side_effect=AssertionError("replacement")):
        response = client.post(url("retry_pdf", invoice))
    assert response.status_code == 302 and "pdf_error=1" in response.url
    assert Invoice.objects.filter(pk=invoice.pk).values().get() == before and counters() == numbers
    assert path.exists() == (damage != "missing")
    if path.exists():
        assert path.read_bytes() == (b"corrupt" if damage == "corrupt" else original)


def test_download_returns_exact_registered_bytes_without_rendering(client, ready, settings):
    invoice = draft(ready)
    issue(client, invoice, review_id(client, invoice))
    invoice.refresh_from_db()
    saved = (settings.RENDERING_ROOT / invoice.rendered_pdf_path).read_bytes()
    with patch("ui.st_invoices.render_pdf", side_effect=AssertionError("GET renders PDF")):
        for parameters, disposition in (({}, "inline"), ({"download": "1"}, "attachment")):
            response = client.get(url("pdf", invoice), parameters)
            assert response.status_code == 200 and response.content == saved
            assert sha256(response.content).hexdigest() == invoice.checksum
            assert response["Content-Type"] == "application/pdf"
            assert response["Content-Disposition"].startswith(disposition + ";")
            assert response["Cache-Control"] == "no-store"


def test_invalid_scope_methods_csrf_and_foreign_evidence(client, ready):
    invoice = draft(ready)
    other = draft(ready)
    proof = review_id(client, other)
    numbers = counters()
    assert issue(client, invoice, proof).status_code == 409
    csrf = Client(enforce_csrf_checks=True)
    for name in ("record_review", "confirm", "retry_pdf"):
        assert csrf.post(url(name, invoice)).status_code == 403
    for name in ("record_review", "retry_pdf"):
        assert client.get(url(name, invoice)).status_code == 405
    for name in ("detail", "retry_pdf", "pdf"):
        method = client.post if name == "retry_pdf" else client.get
        assert method(url(name, invoice)).status_code == 404
    for name in ("review", "confirm", "detail", "pdf"):
        assert client.get(url(name, invoice, company_id=999999)).status_code == 404
    assert counters() == numbers


def test_issued_finances_cannot_be_edited_deleted_or_reset(client, ready):
    invoice = draft(ready)
    issue(client, invoice, review_id(client, invoice))
    invoice.refresh_from_db()
    line = invoice.invoiceline_set.get()
    with pytest.raises((ValidationError, ValueError)):
        spremeni_podatke_racuna(invoice, status="OSNUTEK")
    with pytest.raises((ValidationError, ValueError)):
        spremeni_vrstico(line, opis="Edited")
    with pytest.raises((ValidationError, ValueError)):
        izbrisi_racun(invoice)
    assert client.post(url("detail", invoice), {"action": "edit"}).status_code == 405


def test_setup_and_company_readiness_still_block_issuance(client, ready):
    invoice = draft(ready)
    proof = review_id(client, invoice)
    numbers = counters()
    ready.company.sample_confirmations.all().delete()
    assert issue(client, invoice, proof).status_code == 409
    InitialSetupCompletion.objects.all().delete()
    assert client.get("/").url == reverse("ui:first_run_setup")
    assert issue(client, invoice, proof).status_code == 409
    assert counters() == numbers


def test_database_busy_errors_are_safe_without_another_database_read(client, ready):
    invoice = draft(ready)
    proof = review_id(client, invoice)
    numbers = counters()
    failure = OperationalError("private/database/path and credentials")
    with patch("ui.st_invoices._draft_context", side_effect=failure):
        for name, parameters in (("review", {}), ("confirm", {"review_id": proof})):
            response = client.get(url(name, invoice), parameters)
            assert response.status_code == 409
            assert "Baza je zasedena" in response.content.decode()
            assert b"private/" not in response.content
    with patch("ui.st_invoices.potrdi_osnutek_st", side_effect=failure), patch(
        "ui.st_invoices._draft_context", side_effect=AssertionError("second database read"),
    ):
        response = issue(client, invoice, proof)
    assert response.status_code == 409 and b"private/" not in response.content
    assert counters() == numbers


def test_workflow_has_no_delivery_archive_backup_or_credentials_effects(client, ready, settings):
    invoice = draft(ready)
    with ExitStack() as stack:
        for target in (
            "delivery.services.prepare_attempt", "delivery.services.send_pdf_email",
            "archive.services.archive_pdf", "smtplib.SMTP", "smtplib.SMTP_SSL",
            "keyring.get_password", "keyring.set_password", "rendering.services.render_eslog",
            "archive.configuration.validate_archive_locations",
        ):
            stack.enter_context(patch(target, side_effect=AssertionError(target)))
        assert issue(client, invoice, review_id(client, invoice)).status_code == 302
        assert client.post(url("retry_pdf", invoice)).status_code == 302
        assert client.get(url("pdf", invoice)).status_code == 200
    assert not DeliveryAttempt.objects.exists()
    assert not ArchivedFile.objects.exists() and not BackupSnapshot.objects.exists()
    assert not settings.FEATURE_ESLOG
    invoice.refresh_from_db()
    assert invoice.checksum


def test_atomic_requests_never_wrap_review_confirmation_or_pdf(client, ready, monkeypatch):
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)
    invoice = draft(ready)
    observed = []

    def render_outside_transaction(saved):
        observed.append(connection.in_atomic_block)
        return render_pdf(saved)

    with patch("ui.st_invoices.render_pdf", side_effect=render_outside_transaction):
        assert issue(client, invoice, review_id(client, invoice)).status_code == 302
        assert client.post(url("retry_pdf", invoice)).status_code == 302
    invoice.refresh_from_db()
    assert invoice.checksum and observed == [False, False]
