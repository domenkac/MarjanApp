from datetime import date

import pytest
from django.db import connection
from django.test import Client
from django.urls import reverse

from accounting_export.models import AccountantReport, AccountantReportPdfArtifact
from accounting_export.task_state import accountant_control_dashboard_tasks
from delivery.models import AccountantReportDeliveryAttempt as Attempt
from tests.accounting_export.test_report_snapshot import (
    company as company,  # noqa: PLC0414
)
from tests.accounting_export.test_task_state import (
    accept,
    forbid_side_effects,
    read_only_execute,
)
from tests.accounting_export.test_workflow import AS_OF, PERIOD, Smtp
from tests.accounting_export.test_workflow import setup as setup  # noqa: PLC0414

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def ui_setup(setup, monkeypatch):
    from datetime import UTC, datetime

    from django.utils import timezone

    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 10, 1, 10, tzinfo=UTC))
    monkeypatch.setattr("delivery.credentials.load_password", lambda _: "test-password")
    calls = []
    monkeypatch.setattr(
        "delivery.transport._default_smtp_factory", lambda *_: Smtp(calls)
    )
    return setup, calls


def post(client, company, **changes):
    values = {"year": 2026, "month": 9, "company_id": company.pk} | changes
    return client.post(reverse("ui:accountant_control_send"), values)


def test_post_sends_and_redirects_safe_feedback_then_no_resend(client, ui_setup):
    company, calls = ui_setup
    response = post(client, company)
    assert response.status_code == 302
    assert response.url == "/?accountant_result=ACCEPTED"
    assert len(calls) == 1
    assert (
        "Kontrolni dokument je bil poslan." in client.get(response.url).content.decode()
    )
    assert "Kontrolni dokument za računovodjo ·" not in client.get("/").content.decode()
    post(client, company)
    assert len(calls) == 1 and Attempt.objects.count() == 1


def test_get_read_only_and_post_only_csrf(client, ui_setup, monkeypatch):
    company, calls = ui_setup
    forbid_side_effects(monkeypatch)
    with connection.execute_wrapper(read_only_execute):
        for _ in range(2):
            response = client.get("/")
    content = response.content.decode()
    assert "Pripravi in pošlji" in content
    assert "csrfmiddlewaretoken" in content
    endpoint = reverse("ui:accountant_control_send")
    assert client.get(endpoint).status_code == 405
    assert (
        Client(enforce_csrf_checks=True)
        .post(
            endpoint,
            {
                "company_id": company.pk,
                "year": 2026,
                "month": 9,
            },
        )
        .status_code
        == 403
    )
    assert not calls and not AccountantReport.objects.exists()


@pytest.mark.parametrize(
    "change",
    [
        {"month": 0},
        {"month": 13},
        {"year": 0},
        {"year": 10000},
        {"month": "bad"},
        {"company_id": 99999},
        {"company_id": -1},
        {"year": 2026, "month": 10},
    ],
)
def test_invalid_action_rejected_with_prg(client, ui_setup, change):
    company, calls = ui_setup
    response = post(client, company, **change)
    assert response.status_code == 302
    assert response.url == "/?accountant_result=INVALID_ACTION"
    assert not calls and not AccountantReport.objects.exists()


@pytest.mark.parametrize("state", ["UNKNOWN", "SUBMISSION_STARTED"])
def test_uncertain_rows_have_no_form_and_post_blocks(client, ui_setup, state):
    from accounting_export.services import create_accountant_report

    company, calls = ui_setup
    report = create_accountant_report(company=company, period=PERIOD)
    accept(report, result=state)
    panel = (
        client.get("/")
        .content.decode()
        .split("Aktivna opravila", 1)[1]
        .split("</section>", 1)[0]
    )
    assert "<form" not in panel and "Samodejno ponovno pošiljanje ni na voljo" in panel
    before = list(Attempt.objects.values())
    response = post(client, company)
    assert response.url == f"/?accountant_result={state}"
    assert not calls and list(Attempt.objects.values()) == before


def test_not_ready_has_no_form_and_rejects_post(client, ui_setup):
    from invoicing.models import MonthlyCycle

    company, calls = ui_setup
    MonthlyCycle.objects.update(status=MonthlyCycle.Status.V_TEKU)
    panel = (
        client.get("/")
        .content.decode()
        .split("Aktivna opravila", 1)[1]
        .split("</section>", 1)[0]
    )
    assert "<form" not in panel
    assert post(client, company).url == "/?accountant_result=INVALID_ACTION"
    assert not calls


def test_feedback_whitelist_never_renders_query_details(client, ui_setup):
    content = client.get(
        "/?accountant_result=C:/private/password=secret"
    ).content.decode()
    assert "C:/private" not in content and "password=secret" not in content


def test_task_state_recomputed_after_action(client, ui_setup):
    company, _ = ui_setup
    post(client, company)
    assert not [
        t
        for t in accountant_control_dashboard_tasks(as_of=AS_OF)
        if t.period == date(2026, 9, 1)
    ]
    assert AccountantReportPdfArtifact.objects.count() == 1


def test_current_company_has_no_form_in_partial_task(client, ui_setup):
    from companies.models import Company
    from tests.accounting_export.test_report_snapshot import _invoice

    company, _ = ui_setup
    other = Company.objects.create(naziv="Other", davcna_stevilka="87654321")
    _invoice(other)
    post(client, company)
    response = client.get("/")
    (task,) = response.context["accountant_control_tasks"]
    (current,) = [row for row in task.companies if row.company_id == company.pk]
    assert current.state == "CURRENT" and not current.action_label
    content = (
        response.content.decode()
        .split("Aktivna opravila", 1)[1]
        .split("</section>", 1)[0]
    )
    assert content.count("<form") == 1
    assert f'name="company_id" value="{other.pk}"' in content
    assert f'name="company_id" value="{company.pk}"' not in content


def test_stale_accepted_with_prepared_replacement_uses_continue(
    client, ui_setup, tmp_path
):
    from tests.accounting_export.test_report_snapshot import _invoice, _line
    from tests.accounting_export.test_workflow import prepared

    company, calls = ui_setup
    post(client, company)
    _line(_invoice(company, number="ST-00002", kind="ST"))
    replacement = prepared(company, tmp_path)
    content = (
        client.get("/")
        .content.decode()
        .split("Aktivna opravila", 1)[1]
        .split("</section>", 1)[0]
    )
    assert ">Nadaljuj</button>" in content
    post(client, company, attempt_id=99999)
    replacement.refresh_from_db()
    assert replacement.result == "ACCEPTED" and len(calls) == 2
    assert AccountantReport.objects.count() == 2


def test_ui_unknown_stale_has_no_form(client, ui_setup, tmp_path):
    from tests.accounting_export.test_report_snapshot import _invoice, _line
    from tests.accounting_export.test_workflow import prepared

    company, calls = ui_setup
    post(client, company)
    _line(_invoice(company, number="ST-00002", kind="ST"))
    replacement = prepared(company, tmp_path)
    Attempt.objects.filter(pk=replacement.pk).update(result="UNKNOWN")
    content = (
        client.get("/")
        .content.decode()
        .split("Aktivna opravila", 1)[1]
        .split("</section>", 1)[0]
    )
    assert "Zastarelo" in content and "<form" not in content
    assert "Samodejno ponovno pošiljanje ni na voljo" in content
    assert post(client, company).url == "/?accountant_result=UNKNOWN"
    assert len(calls) == 1 and AccountantReport.objects.count() == 2


@pytest.mark.parametrize(
    "state,current_label",
    [
        ("PREPARED", "Pošlji"),
        ("CONFIRMED_FAILURE", "Ponovi pošiljanje"),
    ],
)
@pytest.mark.parametrize("outdated", [False, True])
def test_prepared_failed_action_label_matches_currentness(
    client,
    ui_setup,
    tmp_path,
    state,
    current_label,
    outdated,
):
    from tests.accounting_export.test_report_snapshot import _invoice, _line
    from tests.accounting_export.test_workflow import prepared

    company, calls = ui_setup
    attempt = prepared(company, tmp_path)
    Attempt.objects.filter(pk=attempt.pk).update(result=state)
    if outdated:
        _line(_invoice(company, number="ST-00002", kind="ST"))
    before = list(Attempt.objects.values())
    response = client.get("/")
    (task,) = response.context["accountant_control_tasks"]
    (row,) = task.companies
    assert row.state == ("PREPARED" if state == "PREPARED" else "FAILED")
    assert row.action_label == ("Nadaljuj" if outdated else current_label)
    assert f">{row.action_label}</button>" in response.content.decode()
    assert not calls and list(Attempt.objects.values()) == before


def test_action_projection_batches_currentness_for_multiple_companies(
    ui_setup, tmp_path
):
    from django.test.utils import CaptureQueriesContext

    from accounting_export.workflow import accountant_control_action_tasks
    from companies.models import Company
    from tests.accounting_export.test_report_snapshot import _invoice, _line
    from tests.accounting_export.test_workflow import prepared

    company, _ = ui_setup
    companies = [company]
    for index in range(3):
        other = Company.objects.create(
            naziv=f"Other {index}",
            davcna_stevilka=str(80000000 + index),
            email_posiljatelj="sender@example.com",
            email_racunovodja="rac@example.com",
        )
        _line(_invoice(other))
        companies.append(other)
    for current in companies:
        prepared(current, tmp_path)
        _line(_invoice(current, number="ST-00002", kind="ST"))
    tasks = accountant_control_dashboard_tasks(as_of=AS_OF)
    with CaptureQueriesContext(connection) as queries:
        projected = accountant_control_action_tasks(tasks)
    assert len(queries) <= 3
    assert [row.action_label for task in projected for row in task.companies] == [
        "Nadaljuj"
    ] * 4
