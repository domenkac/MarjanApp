"""ST recipient confirmation and operational actions never send on GET."""
# ruff: noqa: F401, F811 -- Imported pytest fixtures.

from dataclasses import replace
from unittest.mock import patch

import pytest
from django.db import connection, models
from django.test import Client
from django.urls import reverse

from contracts.models import Tenant
from delivery.configuration import resolve_keyring_credential_ref
from delivery.models import DeliveryAttempt
from delivery.services import mark_submission_started
from tests.delivery.test_delivery_services import FakeSmtp, issued_invoice, smtp_config
from tests.delivery.test_st_delivery import corrupt, prepare, send, st_invoice


def url(name, invoice, attempt=None, **changes):
    values = {"company_id": invoice.company_id, "invoice_id": invoice.pk}
    if attempt:
        values["attempt_id"] = attempt.pk
    return reverse(f"ui:st_{name}", kwargs=values | changes)


def review(client, invoice, **changes):
    return client.post(url("delivery", invoice), {
        "recipients_to": "buyer@example.com", "recipients_cc": "copy@example.com", **changes,
    })


def confirm(client, invoice, response, **changes):
    return client.post(url("prepare_delivery", invoice), {
        "review_token": response.context["confirm_form"].initial["review_token"],
        "confirm_recipients": "on", **changes,
    })


def action(client, invoice, attempt, value, **changes):
    return client.post(url("delivery_action", invoice, attempt), {"action": value, **changes})


def test_get_suggests_tenant_email_but_creates_nothing(client, st_invoice):
    response = client.get(url("delivery", st_invoice))
    assert response.status_code == 200
    assert response.context["form"].initial["recipients_to"] == st_invoice.prejemnik.email
    assert "samo predlog" in response.content.decode()
    assert "Odpri dostavo" in client.get(url("detail", st_invoice)).content.decode()
    assert not DeliveryAttempt.objects.exists()
    assert client.get(url("prepare_delivery", st_invoice)).status_code == 405


@pytest.mark.parametrize("changes", [{"recipients_to": ""}, {"recipients_to": "bad"}, {"recipients_to": "x@example.com\r\nBcc: bad@example.com"}, {"recipients_cc": "bad"}])
def test_invalid_post_cannot_prepare(client, st_invoice, changes):
    response = review(client, st_invoice, **changes)
    assert "confirm_form" not in response.context
    assert not DeliveryAttempt.objects.exists()


def test_final_review_precedes_immutable_preparation_and_send_is_separate(client, st_invoice):
    response = review(client, st_invoice)
    assert not DeliveryAttempt.objects.exists()
    html = response.content.decode()
    for address in ("buyer@example.com", "copy@example.com", "accountant@example.com"):
        assert address in html
    assert "Končni pregled prejemnikov" in html
    assert confirm(client, st_invoice, response, confirm_recipients="").status_code == 409
    assert not DeliveryAttempt.objects.exists()
    assert confirm(client, st_invoice, response).status_code == 302
    attempt = DeliveryAttempt.objects.get()
    assert attempt.result == "PREPARED"
    assert client.get(url("delivery_action", st_invoice, attempt)).status_code == 405
    assert confirm(client, st_invoice, response).status_code == 409
    assert DeliveryAttempt.objects.count() == 1
    assert "Pošlji PDF" in client.get(url("delivery", st_invoice)).content.decode()


def test_manual_recipient_requires_explicit_to_and_no_tenant_changes(client, st_invoice):
    corrupt(st_invoice, prejemnik_id=None)
    before = list(Tenant.objects.values())
    assert client.get(url("delivery", st_invoice)).context["form"].initial["recipients_to"] == ""
    assert "confirm_form" not in review(client, st_invoice, recipients_to="").context
    assert confirm(client, st_invoice, review(client, st_invoice, recipients_to="manual@example.com")).status_code == 302
    assert DeliveryAttempt.objects.get().recipients_to == ["manual@example.com"]
    assert list(Tenant.objects.values()) == before


@pytest.mark.parametrize("fault", ["tamper", "stale", "other_invoice"])
def test_review_token_rejects_tampering_stale_settings_and_other_invoice(client, st_invoice, fault):
    response = review(client, st_invoice)
    token = response.context["confirm_form"].initial["review_token"]
    if fault == "tamper":
        token += "x"
    elif fault == "stale":
        models.QuerySet.update(type(st_invoice.company).objects.filter(pk=st_invoice.company_id), email_racunovodja="changed@example.com")
    else:
        from django.core import signing

        from ui.st_delivery import _SALT

        data = signing.loads(token, salt=_SALT)
        data["invoice_id"] += 1
        token = signing.dumps(data, salt=_SALT)
    assert confirm(client, st_invoice, response, review_token=token).status_code == 409
    assert not DeliveryAttempt.objects.exists()


def test_csrf_and_ownership_protect_all_post_endpoints(st_invoice):
    client = Client(enforce_csrf_checks=True)
    attempt = prepare(st_invoice)
    for endpoint in (url("delivery", st_invoice), url("prepare_delivery", st_invoice), url("delivery_action", st_invoice, attempt)):
        assert client.post(endpoint, {}).status_code == 403
    client = Client()
    assert client.post(url("delivery_action", st_invoice, attempt, company_id=st_invoice.company_id + 1), {"action": "send"}).status_code == 404
    assert client.post(url("delivery_action", st_invoice, attempt, invoice_id=st_invoice.pk + 1), {"action": "send"}).status_code == 404
    assert client.post(url("delivery_action", st_invoice, attempt, attempt_id=attempt.pk + 1), {"action": "send"}).status_code == 404


def test_draft_delivery_endpoints_reject_preparation_and_send(client, st_invoice):
    attempt = prepare(st_invoice)
    corrupt(st_invoice, status="OSNUTEK", stevilka=None)
    assert client.get(url("delivery", st_invoice)).status_code == 404
    assert client.post(url("prepare_delivery", st_invoice), {}).status_code == 404
    assert action(client, st_invoice, attempt, "send").status_code == 404


def test_send_with_atomic_requests_enabled_runs_outside_transaction(client, st_invoice, smtp_config):
    attempt, fake = prepare(st_invoice), FakeSmtp()

    def password(ref):
        assert not connection.in_atomic_block
        return "top-secret"

    with patch.dict(connection.settings_dict, ATOMIC_REQUESTS=True), patch(
        "ui.st_delivery.resolve_smtp_config", return_value=replace(smtp_config, username=attempt.sender),
    ), patch("ui.st_delivery.resolve_keyring_credential_ref", return_value=resolve_keyring_credential_ref(st_invoice.company)), patch(
        "delivery.credentials.load_password", side_effect=password,
    ), patch("delivery.transport._default_smtp_factory", return_value=fake):
        assert action(client, st_invoice, attempt, "send").status_code == 302
        assert action(client, st_invoice, attempt, "send").status_code == 409
    assert sum(call[0] == "data" for call in fake.calls) == 1
    st_invoice.refresh_from_db()
    assert st_invoice.status == "POSLAN"


def test_safe_retry_preparation_never_sends_and_rejects_stale_ids(client, st_invoice, smtp_config):
    source = send(prepare(st_invoice), smtp_config, FakeSmtp({"data": (550, b"rejected")}))
    assert action(client, st_invoice, source, "retry").status_code == 302
    retry = DeliveryAttempt.objects.latest("sequence")
    assert retry.result == "PREPARED" and retry.recipients_to == source.recipients_to
    assert action(client, st_invoice, source, "send").status_code == 409
    assert action(client, st_invoice, source, "retry").status_code == 409
    assert DeliveryAttempt.objects.count() == 2


@pytest.mark.parametrize("decision", ["unknown_sent", "unknown_not_sent"])
def test_unknown_resolution_requires_reason_and_preserves_original_evidence(client, st_invoice, decision):
    source = mark_submission_started(prepare(st_invoice))
    assert action(client, st_invoice, source, "recover").status_code == 302
    original = DeliveryAttempt.objects.values().get(pk=source.pk)
    assert action(client, st_invoice, source, "retry").status_code == 409
    assert action(client, st_invoice, source, decision).status_code == 409
    assert action(client, st_invoice, source, decision, reason="Preverjena mapa Poslano.").status_code == 302
    assert DeliveryAttempt.objects.values().get(pk=source.pk) == original
    assert DeliveryAttempt.objects.count() == (1 if decision == "unknown_sent" else 2)
    if decision == "unknown_sent":
        assert "Pošiljanje je uporabnik izrecno potrdil" in client.get(url("delivery", st_invoice)).content.decode()


def test_raw_errors_and_paths_never_reach_ui(client, st_invoice):
    attempt = prepare(st_invoice)
    secret = "/private/db.sqlite3 password=secret keychain-account"
    with patch("ui.st_delivery.send_pdf_email", side_effect=RuntimeError(secret)), patch(
        "ui.st_delivery.resolve_smtp_config", return_value=object(),
    ), patch("ui.st_delivery.resolve_keyring_credential_ref", return_value=object()):
        response = action(client, st_invoice, attempt, "send")
    html = response.content.decode()
    assert response.status_code == 409 and secret not in html
    assert attempt.pdf_path not in html and st_invoice.company.keychain_kljuc_posiljatelja not in html


def test_cancellation_requires_confirmation_and_reason_then_fresh_review(client, st_invoice):
    original_review = review(client, st_invoice)
    assert confirm(client, st_invoice, original_review).status_code == 302
    attempt = DeliveryAttempt.objects.get()
    assert "Prekliči pripravljeno dostavo" in client.get(url("delivery", st_invoice)).content.decode()
    assert client.get(url("delivery_action", st_invoice, attempt), {"action": "cancel"}).status_code == 405
    for fields in ({}, {"reason": "Popravek"}, {"confirm_cancellation": "on"}, {"reason": "  ", "confirm_cancellation": "on"}):
        assert action(client, st_invoice, attempt, "cancel", **fields).status_code == 409
    with patch.dict(connection.settings_dict, ATOMIC_REQUESTS=True):
        assert action(client, st_invoice, attempt, "cancel", reason="Napačen naslov", confirm_cancellation="on").status_code == 302
    assert DeliveryAttempt.objects.count() == 1
    page = client.get(url("delivery", st_invoice)).content.decode()
    assert "CANCELLED" in page and "Zgodovina dostave" in page
    assert "Preglej prejemnike pred pripravo" in page
    assert "Pošlji PDF" not in page and "Prekliči pripravljeno dostavo" not in page
    assert action(client, st_invoice, attempt, "cancel", reason="Ponovno", confirm_cancellation="on").status_code == 409
    with patch("ui.st_delivery.resolve_smtp_config", return_value=object()), patch(
        "ui.st_delivery.resolve_keyring_credential_ref", return_value=object(),
    ), patch("delivery.credentials.load_password", side_effect=AssertionError("No credential access")):
        assert action(client, st_invoice, attempt, "send").status_code == 409
    assert confirm(client, st_invoice, original_review).status_code == 409
    fresh = review(client, st_invoice, recipients_to="correct@example.com", recipients_cc="new-copy@example.com")
    assert "correct@example.com" in fresh.content.decode()
    assert confirm(client, st_invoice, fresh).status_code == 302
    replacement = DeliveryAttempt.objects.latest("sequence")
    assert replacement.pk != attempt.pk and replacement.sequence == 2
    assert replacement.recipients_to == ["correct@example.com"]
    assert replacement.recipients_cc == ["new-copy@example.com"]
    assert replacement.recipients_bcc == ["accountant@example.com"]
    assert replacement.result == "PREPARED" and replacement.submission_started_at is None
    assert confirm(client, st_invoice, fresh).status_code == 409
    page = client.get(url("delivery", st_invoice)).content.decode()
    assert "Poskus 1 · CANCELLED" in page and "Poskus 2 · PREPARED" in page
    assert "buyer@example.com" in page and "correct@example.com" in page


@pytest.mark.parametrize("result", ["SUBMISSION_STARTED", "ACCEPTED", "CONFIRMED_FAILURE", "UNKNOWN", "CANCELLED"])
def test_ui_rejects_cancellation_of_other_states(client, st_invoice, result):
    attempt = prepare(st_invoice)
    DeliveryAttempt.objects.filter(pk=attempt.pk).update(result=result)
    assert "Prekliči pripravljeno dostavo" not in client.get(url("delivery", st_invoice)).content.decode()
    assert action(client, st_invoice, attempt, "cancel", reason="Popravek", confirm_cancellation="on").status_code == 409
    attempt.refresh_from_db()
    assert attempt.result == result


def test_cancel_endpoint_keeps_csrf_ownership_and_safe_errors(st_invoice):
    attempt = prepare(st_invoice)
    fields = {"action": "cancel", "reason": "Popravek", "confirm_cancellation": "on"}
    assert Client(enforce_csrf_checks=True).post(url("delivery_action", st_invoice, attempt), fields).status_code == 403
    client = Client()
    for changes in ({"company_id": -1}, {"invoice_id": -1}, {"attempt_id": -1}):
        # Positive unknown IDs still match the URL converter.
        changes = {key: 999999 for key in changes}
        assert client.post(url("delivery_action", st_invoice, attempt, **changes), fields).status_code == 404
    secret = "/private/db.sqlite3 password=secret keychain-account"
    with patch("ui.st_delivery.cancel_st_attempt", side_effect=RuntimeError(secret)):
        response = client.post(url("delivery_action", st_invoice, attempt), fields)
    assert response.status_code == 409 and secret not in response.content.decode()
