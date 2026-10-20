"""Operational correction UI and complete HTTP workflows."""
# ruff: noqa: F401, F811 -- Imported pytest fixtures.

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection, models
from django.test import Client
from django.urls import reverse

from archive.models import ArchivedFile
from delivery.models import DeliveryAttempt
from invoicing.models import Invoice
from tests.archive.test_correction_archive import archive_roots
from tests.delivery.test_correction_delivery import (
    FakeSmtp,
    correction_factory,
    pogodba,
)

pytestmark = pytest.mark.django_db(transaction=True)


def url(name, invoice, *args):
    return reverse("ui:correction_" + name, args=[invoice.company_id, invoice.pk, *args])


def review(client, invoice):
    response = client.post(url("delivery", invoice), {"recipients_to": "buyer@example.com", "recipients_cc": "copy@example.com"})
    assert response.status_code == 200
    return response.context["confirm_form"].initial["review_token"]


@pytest.mark.parametrize("kind", ["STORNO", "DOBROPIS"])
@pytest.mark.parametrize("source", ["NP", "standalone", "legacy"])
def test_http_review_prepare_send_archive_end_to_end(client, correction_factory, archive_roots, settings, monkeypatch, kind, source):
    invoice = correction_factory(kind, source)
    settings.MARJAN_SMTP_HOST = "smtp.example.com"
    settings.MARJAN_SMTP_PORT = 587
    settings.MARJAN_SMTP_SECURITY = "STARTTLS"
    settings.MARJAN_SMTP_TIMEOUT_SECONDS = 12.5
    settings.MARJAN_KEYRING_SERVICE = "synthetic"
    fake = FakeSmtp()
    monkeypatch.setattr("delivery.credentials.load_password", lambda ref: "top-secret")
    monkeypatch.setattr("delivery.transport._default_smtp_factory", lambda *a: fake)
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)
    page = client.get(url("detail", invoice))
    assert url("delivery", invoice) in page.content.decode()
    assert client.get(url("delivery", invoice)).status_code == 200
    assert not DeliveryAttempt.objects.exists() and not ArchivedFile.objects.exists()
    token = review(client, invoice)
    data = {"review_token": token, "confirm_recipients": "on"}
    assert client.post(url("prepare_delivery", invoice), data).status_code == 302
    assert not fake.calls
    assert client.post(url("prepare_delivery", invoice), data).status_code == 409
    attempt = DeliveryAttempt.objects.get()
    action = url("delivery_action", invoice, attempt.pk)
    assert client.post(action, {"action": "send"}).status_code == 302
    assert client.post(action, {"action": "send"}).status_code == 409
    assert sum(call[0] == "data" for call in fake.calls) == 1
    assert client.post(url("archive", invoice)).status_code == 302
    assert ArchivedFile.objects.get().status == "VERIFIED"


def test_signed_stale_forged_scope_and_csrf(client, correction_factory):
    invoice = correction_factory()
    token = review(client, invoice)
    secure = Client(enforce_csrf_checks=True)
    for name in ("delivery", "prepare_delivery", "archive"):
        assert secure.post(url(name, invoice)).status_code == 403
    target = url("prepare_delivery", invoice)
    for data in ({"review_token": token}, {"review_token": token + "x", "confirm_recipients": "on"}):
        assert client.post(target, data).status_code == 409
    models.QuerySet.update(type(invoice.company).objects.filter(pk=invoice.company_id), email_racunovodja="changed@example.com")
    assert client.post(target, {"review_token": token, "confirm_recipients": "on"}).status_code == 409
    second = correction_factory()
    assert client.post(url("prepare_delivery", second), {"review_token": token, "confirm_recipients": "on"}).status_code == 409
    assert client.get(reverse("ui:correction_delivery", args=[999999, invoice.pk])).status_code == 404
    assert not DeliveryAttempt.objects.exists()


def test_parallel_duplicate_posts_create_one_attempt(client, correction_factory):
    invoice = correction_factory()
    token = review(client, invoice)
    barrier = Barrier(2)
    def post():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return Client().post(url("prepare_delivery", invoice), {
                "review_token": token, "confirm_recipients": "on",
            }).status_code
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: post(), range(2)))
    assert sorted(results) == [302, 409] and DeliveryAttempt.objects.count() == 1


@pytest.mark.parametrize("action", ["retry", "unknown_sent", "unknown_not_sent", "recover"])
def test_action_scope_csrf_reason_and_history(client, correction_factory, action):
    from delivery.services import (
        mark_confirmed_failure,
        mark_result_unknown,
        mark_submission_started,
    )
    from tests.delivery.test_correction_delivery import prepare
    invoice = correction_factory()
    source = prepare(invoice)
    if action == "retry":
        mark_confirmed_failure(source, code="SMTP_CONNECT_FAILED")
    else:
        source = mark_submission_started(source)
        if action != "recover":
            mark_result_unknown(source, code="SMTP_RESULT_UNKNOWN")
    target = url("delivery_action", invoice, source.pk)
    assert Client(enforce_csrf_checks=True).post(target, {"action": action}).status_code == 403
    other = correction_factory()
    assert client.post(url("delivery_action", other, source.pk), {"action": action, "reason": "Preverjeno"}).status_code == 404
    if action != "recover":
        assert client.post(target, {"action": action, "reason": "..."}).status_code == 409
    assert client.post(target, {"action": action, "reason": "Ponudnik je izrecno preveril oddajo."}).status_code == 302
    assert client.post(target, {"action": action, "reason": "Ponovljeno"}).status_code == 409
    source.refresh_from_db()
    assert source.result == ("CONFIRMED_FAILURE" if action == "retry" else "UNKNOWN")
