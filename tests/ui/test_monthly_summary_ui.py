from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from delivery.credentials import KeyringCredentialRef
from delivery.models import MonthlySummaryAttempt, MonthlySummaryConfiguration
from delivery.services import _monthly_summary_body, _monthly_summary_snapshot
from delivery.transport import SmtpConfig, SmtpResult, SmtpSecurity
from invoicing.models import MonthlyCycle

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def smtp_settings(settings):
    settings.MARJAN_SMTP_HOST = "smtp.example.com"
    settings.MARJAN_SMTP_PORT = 587
    settings.MARJAN_SMTP_SECURITY = "STARTTLS"
    settings.MARJAN_SMTP_TIMEOUT_SECONDS = 10
    settings.MARJAN_KEYRING_SERVICE = "marjan-smtp"


@pytest.fixture(autouse=True)
def archive_state(monkeypatch):
    monkeypatch.setattr("ui.views.resolve_archive_roots", lambda: object())
    monkeypatch.setattr(
        "ui.views.stanje_arhiviranja_mesecnega_cikla",
        lambda _cycle: {
            "selected_count": 0,
            "verified_count": 0,
            "pending_count": 0,
            "issue_count": 0,
            "per_invoice": [],
        },
    )


@pytest.fixture
def configuration():
    return MonthlySummaryConfiguration.objects.create(
        email="user@example.com", keychain_credential_key="summary-key"
    )


@pytest.fixture
def cycle():
    return MonthlyCycle.objects.create(
        obdobje="2026-09-01",
        datum_izdaje="2026-09-30",
        trenutni_korak=MonthlyCycle.Korak.ZAKLJUCEK,
    )


def final_state(pending=0):
    return {
        "headline": "Mesečni postopek zahteva pozornost",
        "requires_attention": True,
        "all_required_work_successful": False,
        "prepared_count": 0,
        "sent_count": 0,
        "intentionally_unissued_count": 0,
        "archive_verified_count": 0,
        "failed_count": 0,
        "unknown_count": 0,
        "delivery_pending_count": pending,
        "archive_pending_count": 0,
        "archive_complete": False,
        "conflict_count": 0,
        "per_company": [],
        "issues": [],
    }


def attempt(cycle, result=MonthlySummaryAttempt.Result.PREPARED, sequence=1):
    from invoicing.services import stanje_zakljucne_kontrole_mesecnega_cikla

    cycle.refresh_from_db()
    state = stanje_zakljucne_kontrole_mesecnega_cikla(cycle)
    state.update(cycle_id=cycle.pk, period=cycle.obdobje)
    snapshot = _monthly_summary_snapshot(cycle, state, timezone.now())
    return MonthlySummaryAttempt.objects.create(
        cycle=cycle,
        sequence=sequence,
        sender="user@example.com",
        recipient="user@example.com",
        subject="Poročilo",
        body=_monthly_summary_body(snapshot),
        summary_snapshot=snapshot,
        result=result,
    )


def successful_final_state():
    state = final_state()
    state.update(
        headline="Mesečni postopek uspešno zaključen",
        requires_attention=False,
        all_required_work_successful=True,
        archive_complete=True,
    )
    return state


def prepare_finalization_ui(monkeypatch, cycle, state):
    monkeypatch.setattr(
        "invoicing.services.stanje_zakljucne_kontrole_mesecnega_cikla",
        lambda _cycle: state,
    )
    monkeypatch.setattr(
        "ui.views.stanje_zakljucne_kontrole_mesecnega_cikla",
        lambda _cycle: state,
    )
    monkeypatch.setattr(
        "ui.views.stanje_dostave_mesecnega_cikla",
        lambda _cycle: {"per_invoice": []},
    )


def test_settings_index_links_to_summary_settings(client):
    content = client.get(reverse("ui:nastavitve")).content.decode()
    assert reverse("ui:nastavitve_zbirno_porocilo") in content
    assert "Zbirno mesečno poročilo" in content


def test_settings_get_shows_only_non_secret_fields(client):
    content = client.get(reverse("ui:nastavitve_zbirno_porocilo")).content.decode()
    assert 'name="email"' in content
    assert 'name="keychain_credential_key"' in content
    assert "s tega e-naslova na isti e-naslov" in content
    assert "ločena od e-naslovov računovodij" in content
    assert 'type="password"' not in content
    assert 'name="password"' not in content


def test_settings_post_creates_and_updates_singleton_with_prg(client):
    url = reverse("ui:nastavitve_zbirno_porocilo")
    response = client.post(
        url, {"email": "first@example.com", "keychain_credential_key": "first-key"}
    )
    assert response.status_code == 302
    assert response.url == f"{url}?shranjeno=1"
    assert MonthlySummaryConfiguration.objects.get(pk=1).email == "first@example.com"

    response = client.post(
        url, {"email": "second@example.com", "keychain_credential_key": "second-key"}
    )
    assert response.status_code == 302
    assert MonthlySummaryConfiguration.objects.count() == 1
    assert MonthlySummaryConfiguration.objects.get(pk=1).email == "second@example.com"
    assert "Nastavitve zbirnega poročila so shranjene" in client.get(response.url).content.decode()


def test_settings_invalid_email_is_inline_and_not_saved(client):
    response = client.post(
        reverse("ui:nastavitve_zbirno_porocilo"),
        {"email": "invalid", "keychain_credential_key": "key"},
    )
    assert response.status_code == 200
    assert "Vnesite veljaven e-poštni naslov." in response.content.decode()
    assert not MonthlySummaryConfiguration.objects.exists()


def test_summary_connection_test_is_post_only(client):
    assert client.get(reverse("ui:test_monthly_summary_smtp_connection")).status_code == 405


def test_summary_connection_test_uses_persisted_identity_without_send(
    client, configuration, monkeypatch
):
    expected_config = SmtpConfig(
        "smtp.example.com", 587, SmtpSecurity.STARTTLS, 10.0, "user@example.com"
    )
    expected_ref = KeyringCredentialRef("marjan-smtp", "summary-key")

    def fake_test(config, credential_ref):
        assert config == expected_config
        assert credential_ref == expected_ref
        return SmtpResult(True, "SMTP_CONNECTION_OK")

    monkeypatch.setattr("ui.views.check_smtp_connection", fake_test)
    monkeypatch.setattr(
        "delivery.transport.send_prepared_email",
        lambda *_args, **_kwargs: pytest.fail("email send called"),
    )
    response = client.post(reverse("ui:test_monthly_summary_smtp_connection"))
    assert response.status_code == 200
    assert "SMTP povezava in prijava sta uspešni" in response.content.decode()
    assert MonthlySummaryAttempt.objects.count() == 0


def test_missing_summary_configuration_skips_network(client, monkeypatch):
    monkeypatch.setattr(
        "ui.views.check_smtp_connection",
        lambda *_args: pytest.fail("network called"),
    )
    response = client.post(reverse("ui:test_monthly_summary_smtp_connection"))
    assert response.status_code == 400
    assert "Nastavitve zbirnega poročila niso veljavne" in response.content.decode()


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("KEYCHAIN_CREDENTIAL_MISSING", "Geslo SMTP ni shranjeno v Keychainu."),
        ("SMTP_AUTH_FAILED", "Prijava v SMTP ni uspela."),
        ("SMTP_CONNECT_FAILED", "SMTP strežnik ni dosegljiv."),
        ("SMTP_TIMEOUT", "Povezava s SMTP strežnikom je potekla."),
        ("SMTP_CONNECTION_LOST", "Povezava s SMTP strežnikom je bila prekinjena."),
    ],
)
def test_summary_connection_failure_is_sanitized(
    client, configuration, monkeypatch, code, message
):
    monkeypatch.setattr(
        "ui.views.check_smtp_connection",
        lambda *_args: SmtpResult(False, code, "secret-value"),
    )
    content = client.post(reverse("ui:test_monthly_summary_smtp_connection")).content.decode()
    assert message in content
    assert "secret-value" not in content


def test_step6_get_is_read_only_and_missing_config_has_link(client, cycle, monkeypatch):
    monkeypatch.setattr("ui.views.stanje_zakljucne_kontrole_mesecnega_cikla", lambda _cycle: final_state())
    for name in (
        "prepare_monthly_summary",
        "prepare_monthly_summary_retry",
        "send_monthly_summary",
        "resolve_monthly_summary_credential_ref",
    ):
        monkeypatch.setattr("ui.views." + name, lambda *_args, _name=name: pytest.fail(f"GET called {_name}"))
    response = client.get(reverse("ui:mesecni_obracun"))
    content = response.content.decode()
    assert response.status_code == 200
    assert "E-naslov za zbirno poročilo ni konfiguriran." in content
    assert reverse("ui:nastavitve_zbirno_porocilo") in content
    assert 'value="send_monthly_summary"' not in content


@pytest.mark.parametrize(("pending", "visible"), [(0, True), (1, False)])
def test_first_send_button_requires_no_pending_delivery(
    client, cycle, configuration, monkeypatch, pending, visible
):
    monkeypatch.setattr("ui.views.stanje_zakljucne_kontrole_mesecnega_cikla", lambda _cycle: final_state(pending))
    content = client.get(reverse("ui:mesecni_obracun")).content.decode()
    assert ('value="send_monthly_summary"' in content) is visible


def test_first_send_prepares_then_sends_authoritative_configuration(
    client, cycle, configuration, monkeypatch
):
    calls = []
    prepared = SimpleNamespace(pk=99)
    smtp_config = object()
    credential_ref = object()
    monkeypatch.setattr("ui.views.stanje_zakljucne_kontrole_mesecnega_cikla", lambda _cycle: final_state())
    monkeypatch.setattr("ui.views.prepare_monthly_summary", lambda received_cycle, received_config: calls.append(("prepare", received_cycle, received_config)) or prepared)
    monkeypatch.setattr("ui.views.resolve_monthly_summary_smtp_config", lambda received: calls.append(("smtp", received)) or smtp_config)
    monkeypatch.setattr("ui.views.resolve_monthly_summary_credential_ref", lambda received: calls.append(("credential", received)) or credential_ref)
    monkeypatch.setattr("ui.views.send_monthly_summary", lambda received, smtp, credential: calls.append(("send", received, smtp, credential)))
    monkeypatch.setattr("ui.views.poslji_dostavo_mesecnega_cikla", lambda *_args: pytest.fail("invoice delivery called"))

    response = client.post(reverse("ui:mesecni_obracun"), {"action": "send_monthly_summary"})
    assert response.status_code == 302
    assert calls[0] == ("prepare", cycle, configuration)
    assert calls[-1] == ("send", prepared, smtp_config, credential_ref)
    assert calls[1][1].email == "user@example.com"
    cycle.refresh_from_db()
    assert cycle.trenutni_korak == MonthlyCycle.Korak.ZAKLJUCEK


def test_existing_prepared_sends_same_attempt_without_prepare(
    client, cycle, configuration, monkeypatch
):
    existing = attempt(cycle)
    sent = []
    monkeypatch.setattr("ui.views.prepare_monthly_summary", lambda *_args: pytest.fail("new attempt prepared"))
    monkeypatch.setattr("ui.views.resolve_monthly_summary_smtp_config", lambda _config: object())
    monkeypatch.setattr("ui.views.resolve_monthly_summary_credential_ref", lambda _config: object())
    monkeypatch.setattr("ui.views.send_monthly_summary", lambda received, *_args: sent.append(received.pk))
    response = client.post(reverse("ui:mesecni_obracun"), {"action": "send_monthly_summary"})
    assert response.status_code == 302
    assert sent == [existing.pk]
    assert MonthlySummaryAttempt.objects.count() == 1


@pytest.mark.parametrize(
    ("state", "label", "button"),
    [
        (MonthlySummaryAttempt.Result.ACCEPTED, "Zbirno poročilo poslano", None),
        (MonthlySummaryAttempt.Result.CONFIRMED_FAILURE, "Pošiljanje zbirnega poročila ni uspelo", "Ponovi pošiljanje zbirnega poročila"),
        (MonthlySummaryAttempt.Result.UNKNOWN, "Rezultat pošiljanja zbirnega poročila ni znan", None),
        (MonthlySummaryAttempt.Result.SUBMISSION_STARTED, "Pošiljanje v teku – rezultat je treba preveriti", None),
    ],
)
def test_step6_attempt_states(client, cycle, configuration, monkeypatch, state, label, button):
    attempt(cycle, state)
    monkeypatch.setattr("ui.views.stanje_zakljucne_kontrole_mesecnega_cikla", lambda _cycle: final_state())
    content = client.get(reverse("ui:mesecni_obracun")).content.decode()
    assert label in content
    assert (button in content) if button else ('value="send_monthly_summary"' not in content)


def test_confirmed_failure_retry_sends_only_new_attempt(
    client, cycle, configuration, monkeypatch
):
    failed = attempt(cycle, MonthlySummaryAttempt.Result.CONFIRMED_FAILURE)
    sent = []
    monkeypatch.setattr("ui.views.resolve_monthly_summary_smtp_config", lambda _config: object())
    monkeypatch.setattr("ui.views.resolve_monthly_summary_credential_ref", lambda _config: object())
    monkeypatch.setattr("ui.views.send_monthly_summary", lambda received, *_args: sent.append(received.pk))
    response = client.post(reverse("ui:mesecni_obracun"), {"action": "send_monthly_summary"})
    assert response.status_code == 302
    retry = MonthlySummaryAttempt.objects.get(sequence=2)
    assert MonthlySummaryAttempt.objects.count() == 2
    assert retry.sender == failed.sender
    assert sent == [retry.pk]


def test_confirmed_failure_changed_email_does_not_create_or_send(
    client, cycle, configuration, monkeypatch
):
    failed = attempt(cycle, MonthlySummaryAttempt.Result.CONFIRMED_FAILURE)
    configuration.email = "new@example.com"
    configuration.save()
    monkeypatch.setattr(
        "ui.views.prepare_monthly_summary_retry",
        lambda *_args: pytest.fail("retry prepared"),
    )
    monkeypatch.setattr(
        "ui.views.send_monthly_summary", lambda *_args: pytest.fail("summary sent")
    )

    response = client.post(
        reverse("ui:mesecni_obracun"),
        {"action": "send_monthly_summary"},
        follow=True,
    )

    assert MonthlySummaryAttempt.objects.count() == 1
    assert MonthlySummaryAttempt.objects.order_by("-sequence").first() == failed
    assert "Nastavitve zbirnega poročila niso veljavne." in response.content.decode()
    assert "Traceback" not in response.content.decode()


@pytest.mark.parametrize("invalid", [False, True])
def test_confirmed_failure_missing_or_invalid_configuration_does_not_create_or_send(
    client, cycle, configuration, monkeypatch, invalid
):
    failed = attempt(cycle, MonthlySummaryAttempt.Result.CONFIRMED_FAILURE)
    if invalid:
        MonthlySummaryConfiguration.objects.filter(pk=1).update(email="invalid")
    else:
        configuration.delete()
    monkeypatch.setattr(
        "ui.views.prepare_monthly_summary_retry",
        lambda *_args: pytest.fail("retry prepared"),
    )
    monkeypatch.setattr(
        "ui.views.send_monthly_summary", lambda *_args: pytest.fail("summary sent")
    )

    response = client.post(
        reverse("ui:mesecni_obracun"),
        {"action": "send_monthly_summary"},
        follow=True,
    )

    assert MonthlySummaryAttempt.objects.count() == 1
    assert MonthlySummaryAttempt.objects.first() == failed
    assert "Nastavitve zbirnega poročila niso veljavne." in response.content.decode()


def test_confirmed_failure_changed_keychain_key_still_retries(
    client, cycle, configuration, monkeypatch
):
    attempt(cycle, MonthlySummaryAttempt.Result.CONFIRMED_FAILURE)
    configuration.keychain_credential_key = "replacement-key"
    configuration.save()
    credential_refs = []
    sent = []
    monkeypatch.setattr("ui.views.resolve_monthly_summary_smtp_config", lambda _config: object())
    monkeypatch.setattr(
        "ui.views.resolve_monthly_summary_credential_ref",
        lambda received: credential_refs.append(received.keychain_credential_key)
        or object(),
    )
    monkeypatch.setattr(
        "ui.views.send_monthly_summary", lambda received, *_args: sent.append(received.pk)
    )

    response = client.post(
        reverse("ui:mesecni_obracun"), {"action": "send_monthly_summary"}
    )

    retry = MonthlySummaryAttempt.objects.get(sequence=2)
    assert response.status_code == 302
    assert credential_refs == ["replacement-key"]
    assert sent == [retry.pk]


def test_summary_send_error_is_safe(client, cycle, configuration, monkeypatch):
    existing = attempt(cycle)
    monkeypatch.setattr("ui.views.resolve_monthly_summary_smtp_config", lambda _config: object())
    monkeypatch.setattr("ui.views.resolve_monthly_summary_credential_ref", lambda _config: object())
    monkeypatch.setattr("ui.views.send_monthly_summary", lambda *_args: (_ for _ in ()).throw(RuntimeError("secret-value")))
    response = client.post(reverse("ui:mesecni_obracun"), {"action": "send_monthly_summary"}, follow=True)
    content = response.content.decode()
    assert existing.pk
    assert "Zbirnega poročila ni bilo mogoče poslati." in content
    assert "secret-value" not in content


def test_step6_final_button_requires_successful_control_and_accepted_summary(
    client, monkeypatch, cycle, configuration
):
    prepare_finalization_ui(monkeypatch, cycle, successful_final_state())
    attempt(cycle, MonthlySummaryAttempt.Result.ACCEPTED)

    content = client.get(reverse("ui:mesecni_obracun")).content.decode()

    assert "Zaključi mesečni cikel" in content


@pytest.mark.parametrize(
    ("state", "result"),
    [
        (final_state(), MonthlySummaryAttempt.Result.ACCEPTED),
        (successful_final_state(), MonthlySummaryAttempt.Result.PREPARED),
    ],
)
def test_step6_final_button_stays_hidden_when_not_eligible(
    client, monkeypatch, cycle, configuration, state, result
):
    prepare_finalization_ui(monkeypatch, cycle, state)
    attempt(cycle, result)

    content = client.get(reverse("ui:mesecni_obracun")).content.decode()

    assert "Zaključi mesečni cikel" not in content


def test_step6_get_never_finalizes(client, monkeypatch, cycle, configuration):
    prepare_finalization_ui(monkeypatch, cycle, successful_final_state())
    attempt(cycle, MonthlySummaryAttempt.Result.ACCEPTED)
    monkeypatch.setattr(
        "ui.views.zakljuci_mesecni_cikel",
        lambda *_args: pytest.fail("GET ne sme zaključiti cikla."),
    )

    assert client.get(reverse("ui:mesecni_obracun")).status_code == 200


def test_finalize_post_calls_only_finalization_service_once_and_redirects(
    client, monkeypatch, cycle
):
    calls = []
    monkeypatch.setattr(
        "ui.views.zakljuci_mesecni_cikel",
        lambda received: calls.append(received.pk),
    )
    for name in (
        "poslji_dostavo_mesecnega_cikla",
        "arhiviraj_poslane_racune_mesecnega_cikla",
        "send_monthly_summary",
    ):
        monkeypatch.setattr(
            f"ui.views.{name}",
            lambda *_args, **_kwargs: pytest.fail(
                "Finalizacija ne sme izvajati drugih operacij."
            ),
        )

    response = client.post(
        reverse("ui:mesecni_obracun"), {"action": "finalize_monthly_cycle"}
    )

    cycle.refresh_from_db()
    assert calls == [cycle.pk]
    assert response.status_code == 302
    assert response.url == reverse("ui:dashboard")
    assert cycle.status == MonthlyCycle.Status.V_TEKU


def test_finalize_backend_rejection_keeps_cycle_open_and_shows_safe_error(
    client, monkeypatch, cycle, configuration
):
    prepare_finalization_ui(monkeypatch, cycle, successful_final_state())
    attempt(cycle, MonthlySummaryAttempt.Result.ACCEPTED)

    def reject(_cycle):
        raise ValidationError("/secret/path SMTP password=hidden")

    monkeypatch.setattr("ui.views.zakljuci_mesecni_cikel", reject)

    response = client.post(
        reverse("ui:mesecni_obracun"), {"action": "finalize_monthly_cycle"}
    )

    cycle.refresh_from_db()
    content = response.content.decode()
    assert response.status_code == 200
    assert cycle.status == MonthlyCycle.Status.V_TEKU
    assert "Mesečnega cikla ni bilo mogoče zaključiti." in content
    assert "/secret/path" not in content
    assert "password" not in content
