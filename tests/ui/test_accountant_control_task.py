from datetime import date

import pytest
from django.db import connection

from accounting_export.models import AccountantReport
from accounting_export.services import create_accountant_report
from accounting_export.task_state import (
    accountant_control_dashboard_tasks,
    accountant_control_task_state,
)
from delivery.models import AccountantReportDeliveryAttempt as Attempt
from invoicing.models import Invoice, MonthlyBatch, MonthlyCycle
from tests.accounting_export.test_report_snapshot import _invoice
from tests.accounting_export.test_report_snapshot import (
    company as company,  # noqa: PLC0414
)
from tests.accounting_export.test_task_state import (
    accept,
    completed_period,
    forbid_side_effects,
    read_only_execute,
)

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("expected,label", [
    ("ODPRTO", "Odprto"), ("DELNO", "Delno"),
    ("OPOZORILO", "Opozorilo"), ("ZASTARELO", "Zastarelo"),
    ("ZAKLJUCENO", "Zaključeno"),
])
def test_dashboard_states(client, company, monkeypatch, expected, label):
    from companies.models import Company

    _invoice(company)
    report = create_accountant_report(company=company, period=date(2026, 9, 1))
    if expected in ("DELNO", "ZASTARELO", "ZAKLJUCENO"):
        accept(report)
    if expected == "DELNO":
        other = Company.objects.create(naziv="Other", davcna_stevilka="87654321")
        _invoice(other)
    if expected == "ZASTARELO":
        _invoice(company, number="STORNO-00002", kind="STORNO")
    as_of = date(2026, 10, 6 if expected == "OPOZORILO" else 1)
    task = accountant_control_task_state(as_of=as_of)
    assert task.state == expected
    monkeypatch.setattr("ui.views.accountant_control_dashboard_tasks", lambda: (task,) if task.active else ())
    before = {
        model: list(model.objects.values())
        for model in (Invoice, MonthlyCycle, MonthlyBatch, AccountantReport, Attempt)
    }
    forbid_side_effects(monkeypatch)
    with connection.execute_wrapper(read_only_execute):
        response = client.get("/")
    content = response.content.decode()
    assert response.status_code == 200
    assert before == {model: list(model.objects.values()) for model in before}
    if expected == "ZAKLJUCENO":
        assert "Kontrolni dokument za računovodjo" not in content
        assert "Ni aktivnih opravil" in content
    else:
        assert "Kontrolni dokument za računovodjo" in content
        assert label in content
        assert "09/2026" in content and "05. 10. 2026" in content
        assert "Zgodovinska Alfa" in content
        assert task.companies[0].state_label in content
        assert "Ni aktivnih opravil" not in content
        if expected in ("OPOZORILO", "ZASTARELO"):
            assert 'role="alert"' in content
    # Existing panels/actions remain in the same GET response.
    assert "Potrebne potrditve" in content
    assert "Zadnji zaključen mesečni obračun" in content
    assert 'href="/mesecni-obracun/"' in content
    assert "Začni mesečni obračun" in content
    task_panel = content.split("Aktivna opravila", 1)[1].split("</section>", 1)[0]
    assert "<form" not in task_panel and "<button" not in task_panel and "<a " not in task_panel


@pytest.mark.parametrize("result,label", [
    ("PREPARED", "Pripravljeno, še ni poslano"),
    ("SUBMISSION_STARTED", "Pošiljanje v teku"),
    ("CONFIRMED_FAILURE", "Pošiljanje ni uspelo"),
    ("UNKNOWN", "Rezultat pošiljanja ni znan"),
])
def test_diagnostics_visible(client, company, monkeypatch, result, label):
    _invoice(company)
    report = create_accountant_report(company=company, period=date(2026, 9, 1))
    accept(report, result=result)
    monkeypatch.setattr("ui.views.accountant_control_dashboard_tasks", lambda: accountant_control_dashboard_tasks(as_of=date(2026, 10, 1)))
    assert label in client.get("/").content.decode()


def test_real_dashboard_service_read_only_when_evidence_missing(client, monkeypatch):
    forbid_side_effects(monkeypatch)
    with connection.execute_wrapper(read_only_execute):
        response = client.get("/")
    task, = response.context["accountant_control_tasks"]
    assert not task.ready and task.active
    assert task.issue_label in response.content.decode()
    assert AccountantReport.objects.count() == 0 and Attempt.objects.count() == 0


def test_dashboard_renders_multiple_periods_and_filters_completed_history(client, company, monkeypatch):
    completed_period(company, date(2026, 8, 1))
    _invoice(company)
    report = create_accountant_report(company=company, period=date(2026, 9, 1))
    accept(report)
    _invoice(company, number="STORNO-00002", kind="STORNO")
    # A completed, current historical report must remain hidden.
    completed_period(company, date(2026, 7, 1))
    july = AccountantReport.objects.create(
        company=company, period=date(2026, 7, 1), generated_at=report.generated_at,
        schema_version=report.schema_version, snapshot={"issued_rows": []},
    )
    accept(july)
    monkeypatch.setattr("ui.views.accountant_control_dashboard_tasks", lambda: accountant_control_dashboard_tasks(as_of=date(2026, 11, 1)))
    before = {
        model: list(model.objects.values())
        for model in (Invoice, MonthlyCycle, MonthlyBatch, AccountantReport, Attempt)
    }
    forbid_side_effects(monkeypatch)
    with connection.execute_wrapper(read_only_execute):
        response = client.get("/")
    tasks = response.context["accountant_control_tasks"]
    assert [(task.period.month, task.state) for task in tasks] == [
        (8, "OPOZORILO"), (9, "ZASTARELO"), (10, "ODPRTO"),
    ]
    content = response.content.decode().split("Aktivna opravila", 1)[1].split("</section>", 1)[0]
    assert content.count("Kontrolni dokument za računovodjo") == 3
    assert content.index("08/2026") < content.index("09/2026") < content.index("10/2026")
    assert "07/2026" not in content
    for value in ("05. 09. 2026", "07. 09. 2026", "05. 10. 2026", "05. 11. 2026"):
        assert value in content
    for task in tasks:
        assert task.state_label in content
        if task.issue_label:
            assert task.issue_label in content
        for row in task.companies:
            assert row.label in content and row.state_label in content
    assert "<a " not in content and "<button" not in content and "<form" not in content
    assert before == {model: list(model.objects.values()) for model in before}
