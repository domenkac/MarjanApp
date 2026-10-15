"""Real two-company workflows; only SMTP and credential boundaries are fake."""
import hashlib
import json
import re
import shutil
import socket
import sqlite3
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

import keyring
import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.urls import reverse

from accounting_export.csv_renderer import render_accountant_report_csv
from accounting_export.models import AccountantReport, AccountantReportPdfArtifact
from accounting_export.workflow import deliver_accountant_control_report
from archive.configuration import verify_and_save_archive_roots
from archive.models import ArchivedFile, BackupSnapshot
from companies.models import NumberSequence
from companies.readiness import verify_numbering
from companies.services import potrdi_ddv_nastavitve
from contracts.models import Lease, RecipientContact
from core.models import AuditLogEntry
from delivery.models import (
    DeliveryAttempt,
    MonthlySummaryAttempt,
    MonthlySummaryConfiguration,
)
from delivery.services import prepare_retry_attempt
from invoicing import services
from invoicing.models import (
    Invoice,
    InvoiceLine,
    MonthlyBatch,
    MonthlyCandidateReview,
    MonthlyCycle,
)
from tests.accounting_export.test_workflow import Smtp
from tests.business_fixtures import save_business_record
from tests.financial_fixtures import save_financial_fixture, update_financial_fixture
from tests.invoicing.test_preflight import _add_ready_company
from tests.invoicing.test_preflight import (
    ready_company as ready_company,  # noqa: PLC0414
)
from tests.setup_helpers import approve_sample, prepare_initial_setup

pytestmark = pytest.mark.django_db(transaction=True)
PERIOD = date(2030, 11, 1)
ISSUE_DATE = date(2030, 11, 10)


@pytest.fixture(scope="module")
def django_db_setup(django_db_blocker, tmp_path_factory, django_test_environment):
    # Keep this module isolated even when the full suite already has a test DB.
    from django.test.utils import setup_databases, teardown_databases

    original = connection.settings_dict["TEST"]["NAME"]
    connection.settings_dict["TEST"]["NAME"] = str(
        tmp_path_factory.mktemp("two-company-db") / "test.sqlite3"
    )
    with django_db_blocker.unblock():
        configuration = setup_databases(verbosity=0, interactive=False)
    try:
        yield
    finally:
        with django_db_blocker.unblock():
            teardown_databases(configuration, verbosity=0)
        connection.settings_dict["TEST"]["NAME"] = original


@pytest.fixture
def external_boundaries(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Real network or Keychain access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    for name in ("get_password", "set_password", "delete_password", "get_credential"):
        monkeypatch.setattr(keyring, name, forbidden)
    monkeypatch.setattr("delivery.credentials.load_password", lambda _: "test-password")
    # Include backup working files and monthly lock files in the isolated root.
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(work))
    return work


@pytest.fixture(autouse=True)
def forbid_external_access(external_boundaries):
    """Install guards before shared readiness fixtures render sample PDFs."""


@pytest.fixture
def flow(ready_company, client, settings, tmp_path, monkeypatch, external_boundaries):
    assert connection.get_autocommit() and not connection.in_atomic_block
    assert "two-company-db" in str(connection.settings_dict["NAME"])
    work = external_boundaries

    # The shared fixture creates real assets and confirmations, plus a synthetic
    # cycle. Remove only that test cycle so the UI performs the actual start.
    data = ready_company
    beta, _ = _add_ready_company(data)
    data["cycle"].batches.all().delete()
    data["cycle"].delete()
    media = tmp_path / "media"
    existing = list(tmp_path.iterdir())
    media.mkdir()
    for source in existing:
        if source != work:
            shutil.move(str(source), str(media / source.name))
    settings.MEDIA_ROOT = media
    settings.RENDERING_ROOT = tmp_path / "rendered"
    roots = [tmp_path / name for name in ("local", "icloud", "dropbox")]
    for root in roots:
        root.mkdir()
    verify_and_save_archive_roots(*roots)
    for name, value in {
        "MARJAN_SMTP_HOST": "smtp.example.invalid",
        "MARJAN_SMTP_PORT": 587,
        "MARJAN_SMTP_SECURITY": "STARTTLS",
        "MARJAN_SMTP_TIMEOUT_SECONDS": 10,
        "MARJAN_KEYRING_SERVICE": "isolated-e2e",
    }.items():
        setattr(settings, name, value)
    MonthlySummaryConfiguration.objects.create(
        email="owner@example.com", keychain_credential_key="owner-test"
    )
    companies = [data["company"], beta]
    leases = [data["lease"], Lease.objects.get(unit__property__company=beta)]
    for index, (company, lease) in enumerate(zip(companies, leases, strict=True)):
        company.ddv_zavezanec = True
        company.email_posiljatelj = f"issuer-{index}@example.com"
        company.email_racunovodja = f"accountant-{index}@example.com"
        save_financial_fixture(company)
        lease.tenant.identificiran_za_ddv = True
        save_business_record(lease.tenant)
        RecipientContact.objects.create(
            lease=lease,
            email=f"tenant-{index}@example.com",
            vloga=RecipientContact.Vloga.TO,
            kanal=RecipientContact.Kanal.PDF,
        )
        potrdi_ddv_nastavitve(company)
        approve_sample(company, lease)
        verify_numbering(company, np_last=40 + index * 50, st_last=0, st_year=2026, explicit=True)
    prepare_initial_setup(companies[0], leases[0])
    calls = []
    outcomes = {}
    envelopes = []

    class RecordedSmtp(Smtp):
        def mail(self, sender):
            self.sender = sender
            self.recipients = []
            return super().mail(sender)

        def rcpt(self, recipient):
            self.recipients.append(recipient)
            return super().rcpt(recipient)

        def data(self, payload):
            envelopes.append((self.sender, self.recipients.copy()))
            return super().data(payload)

    def smtp_factory(config, context):
        assert config.host == "smtp.example.invalid"
        return RecordedSmtp(calls, outcome=outcomes.get(config.username, "accepted"))

    monkeypatch.setattr("delivery.transport._default_smtp_factory", smtp_factory)
    return {
        "client": client,
        "companies": companies,
        "leases": leases,
        "calls": calls,
        "outcomes": outcomes,
        "root": tmp_path,
        "roots": roots,
        "media": media,
        "envelopes": envelopes,
    }


def _post(flow, action=None, **values):
    if action:
        values["action"] = action
    response = flow["client"].post(reverse("ui:mesecni_obracun"), values)
    assert response.status_code == 302, response.content.decode()
    assert "error" not in response.url, response.url
    return response


def _pdf(path, root):
    path = Path(path)
    assert path.resolve().is_relative_to(root.resolve())
    content = path.read_bytes()
    assert content.startswith(b"%PDF-")
    assert len(content) > 1000 and content.rstrip().endswith(b"%%EOF")
    # WeasyPrint emits an xref stream; check its actual trailer offset.
    trailer = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*$", content)
    assert trailer is not None
    offset = int(trailer.group(1))
    assert re.match(rb"\d+ \d+ obj", content[offset:])
    assert b"/Type /XRef" in content[offset : offset + 500]
    return content


def _rendered(flow, invoice):
    return flow["root"] / "rendered" / invoice.rendered_pdf_path


def _hash(content):
    return hashlib.sha256(content).hexdigest()


def _issued_content(invoice):
    # Delivery/artifact metadata can change; all fiscal snapshots must remain fixed.
    excluded = {"status", "rendered_pdf_path", "checksum", "pdf_generated_at"}
    return {
        field.name: getattr(invoice, field.attname)
        for field in invoice._meta.concrete_fields
        if field.name not in excluded
    }, list(InvoiceLine.objects.filter(invoice=invoice).values())


def _start_and_issue(flow):
    _post(flow, leto=2030, mesec=11, datum_izdaje=ISSUE_DATE.isoformat())
    cycle = MonthlyCycle.objects.get()
    assert cycle.obdobje == PERIOD and cycle.datum_izdaje == ISSUE_DATE
    assert BackupSnapshot.objects.get().trigger == "PRE_MONTHLY"
    _post(flow, leto=2030, mesec=11, datum_izdaje=ISSUE_DATE.isoformat())
    assert MonthlyCycle.objects.count() == BackupSnapshot.objects.count() == 1
    assert set(cycle.batches.values_list("company_id", flat=True)) == {
        company.pk for company in flow["companies"]
    }
    assert (
        list(cycle.batches.values_list("preflight_status", flat=True)) == ["PASSED"] * 2
    )
    _post(flow, "continue_step3")
    assert (
        list(cycle.batches.values_list("calculation_status", flat=True))
        == ["CALCULATED"] * 2
    )
    snapshots = list(
        cycle.batches.order_by("company_id").values_list(
            "calculation_snapshot", flat=True
        )
    )
    _post(flow, "rerun_calculation")
    assert (
        list(
            cycle.batches.order_by("company_id").values_list(
                "calculation_snapshot", flat=True
            )
        )
        == snapshots
    )
    _post(flow, "continue_step4")
    reviews = list(MonthlyCandidateReview.objects.order_by("lease_id"))
    assert len(reviews) == 2 and all(review.selected for review in reviews)
    assert any(review.requires_confirmation for review in reviews)
    for review in reviews:
        if review.requires_confirmation:
            _post(
                flow,
                "confirm_candidate",
                lease_id=review.lease_id,
                candidate_fingerprint=review.candidate_fingerprint,
            )
    _post(flow, "freeze_package")
    cycle.refresh_from_db()
    assert cycle.trenutni_korak == MonthlyCycle.Korak.POTRDITEV_PAKETA
    mutations = []

    def observe_issuance(execute, sql, params, many, context):
        for table, operation in (
            ("invoicing_invoice", "INSERT"),
            ("invoicing_invoiceline", "INSERT"),
            ("companies_numbersequence", "UPDATE"),
        ):
            if sql.startswith(operation) and f'"{table}"' in sql:
                assert connection.in_atomic_block
                mutations.append(table)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(observe_issuance):
        _post(flow, "issue_frozen_package")
    assert mutations.count("invoicing_invoice") == 2
    assert mutations.count("invoicing_invoiceline") == 2
    assert mutations.count("companies_numbersequence") == 2
    invoices = list(Invoice.objects.order_by("company_id"))
    assert len(invoices) == 2
    assert [invoice.stevilka for invoice in invoices] == ["NP-00041", "NP-00091"]
    assert [invoice.skupaj_z_ddv for invoice in invoices] == [
        Decimal("122.00"),
        Decimal("97.60"),
    ]
    _post(flow, "issue_frozen_package")
    assert Invoice.objects.count() == 2
    _post(flow, "render_final_pdfs")
    cycle.refresh_from_db()
    assert cycle.trenutni_korak == MonthlyCycle.Korak.ZAKLJUCEK
    assert cycle.status == MonthlyCycle.Status.V_TEKU and cycle.completed_at is None
    for index, invoice in enumerate(invoices):
        invoice.refresh_from_db()
        assert invoice.company_id == flow["companies"][index].pk
        assert invoice.lease_id == flow["leases"][index].pk
        assert invoice.izdajatelj_naziv == flow["companies"][index].naziv
        assert invoice.prejemnik_naziv == flow["leases"][index].tenant.naziv
        assert _hash(_pdf(_rendered(flow, invoice), flow["root"])) == invoice.checksum
        with pytest.raises(ValueError, match="osnutek"):
            services.spremeni_podatke_racuna(invoice, prejemnik_naziv="Forbidden")
    flow.update(
        cycle=cycle,
        invoices=invoices,
        fiscal=[_issued_content(invoice) for invoice in invoices],
        snapshots=snapshots,
    )
    _post(flow, "prepare_delivery")
    assert DeliveryAttempt.objects.count() == 2


def _assert_delivery(flow):
    for index, invoice in enumerate(flow["invoices"]):
        attempts = list(
            DeliveryAttempt.objects.filter(invoice=invoice).order_by("sequence")
        )
        assert [attempt.sequence for attempt in attempts] == list(
            range(1, len(attempts) + 1)
        )
        for attempt in attempts:
            assert attempt.sender == f"issuer-{index}@example.com"
            assert attempt.recipients_to == [f"tenant-{index}@example.com"]
            assert attempt.recipients_bcc == [f"accountant-{index}@example.com"]
            assert attempt.recipients_cc == []
            assert (attempt.pdf_path, attempt.pdf_checksum) == (
                invoice.rendered_pdf_path,
                invoice.checksum,
            )
        messages = [
            message
            for message in flow["calls"]
            if message["From"] == f"issuer-{index}@example.com"
        ]
        assert len(messages) == len(attempts)
        for message in messages:
            attachments = list(message.iter_attachments())
            assert len(attachments) == 1
            assert (
                attachments[0].get_payload(decode=True)
                == _rendered(flow, invoice).read_bytes()
            )
            assert message["To"] == f"tenant-{index}@example.com"
            assert message["Bcc"] is None
        envelopes = [
            recipients
            for sender, recipients in flow["envelopes"]
            if sender == f"issuer-{index}@example.com"
        ]
        assert envelopes == [
            [f"tenant-{index}@example.com", f"accountant-{index}@example.com"]
        ] * len(attempts)


def _assert_archive(flow):
    assert ArchivedFile.objects.count() == 2
    paths = set()
    for invoice in flow["invoices"]:
        archived = ArchivedFile.objects.get(invoice=invoice)
        assert archived.status == "VERIFIED" and archived.checksum == invoice.checksum
        for path, root in zip(
            (archived.local_path, archived.icloud_path), flow["roots"][:2], strict=True
        ):
            path = Path(path)
            assert path.is_relative_to(root)
            assert invoice.izdajatelj_davcna_stevilka in str(path.relative_to(root))
            assert path.name == f"{invoice.stevilka}.pdf"
            assert _hash(_pdf(path, flow["root"])) == archived.checksum
            assert path.read_bytes() == _rendered(flow, invoice).read_bytes()
            paths.add(path)
    assert len(paths) == 4


def _assert_backups(flow):
    assert set(BackupSnapshot.objects.values_list("trigger", flat=True)) == {
        "PRE_MONTHLY",
        "POST_MONTHLY",
    }
    assert BackupSnapshot.objects.count() == 2
    for snapshot in BackupSnapshot.objects.all():
        path = Path(snapshot.path)
        assert path.is_relative_to(flow["roots"][2])
        assert _hash(path.read_bytes()) == snapshot.zip_checksum
        with ZipFile(path) as archive:
            assert archive.testzip() is None
            manifest = json.loads(archive.read("manifest.json"))
            assert (
                manifest["format_version"] == 1
                and manifest["trigger"] == snapshot.trigger
            )
            assert manifest["created_at"] == snapshot.created_at.isoformat()
            assert manifest["source_roots"] == dict(
                zip(
                    ("local_root", "icloud_root", "dropbox_root"),
                    map(str, flow["roots"]),
                    strict=True,
                )
            )
            db = archive.read("database/db.sqlite3")
            assert (
                _hash(db) == manifest["database_checksum"] == snapshot.database_checksum
            )
            for asset in flow["media"].rglob("*"):
                if asset.is_file():
                    assert (
                        archive.read(
                            "media/" + asset.relative_to(flow["media"]).as_posix()
                        )
                        == asset.read_bytes()
                    )
            if snapshot.trigger == "POST_MONTHLY":
                for artifact in ArchivedFile.objects.all():
                    local = Path(artifact.local_path)
                    assert (
                        _hash(
                            archive.read(
                                "archive/"
                                + local.relative_to(flow["roots"][0]).as_posix()
                            )
                        )
                        == artifact.checksum
                    )
            copied = flow["root"] / f"{snapshot.trigger}.sqlite3"
            copied.write_bytes(db)
        with sqlite3.connect(copied) as database:
            assert database.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert database.execute(
                "SELECT COUNT(*) FROM invoicing_invoice"
            ).fetchone() == (0 if snapshot.trigger == "PRE_MONTHLY" else 2,)
            if snapshot.trigger == "POST_MONTHLY":
                status, evidence = database.execute(
                    "SELECT status, completion_snapshot FROM invoicing_monthlycycle"
                ).fetchone()
                assert status == "ZAKLJUCEN" and json.loads(evidence)["sent_count"] == 2


def _finish(flow):
    _post(flow, "archive_sent_invoices")
    _assert_archive(flow)
    before = list(ArchivedFile.objects.values())
    _post(flow, "archive_sent_invoices")
    assert list(ArchivedFile.objects.values()) == before
    state = services.stanje_zakljucne_kontrole_mesecnega_cikla(flow["cycle"])
    assert state["all_required_work_successful"] and not state["requires_attention"]
    assert state["sent_count"] == state["archive_verified_count"] == 2
    assert {
        row["company_id"]: (row["sent_count"], row["archive_verified_count"])
        for row in state["per_company"]
    } == {company.pk: (1, 1) for company in flow["companies"]}
    _post(flow, "send_monthly_summary")
    summary = MonthlySummaryAttempt.objects.get()
    assert summary.result == "ACCEPTED" and summary.recipient == "owner@example.com"
    assert summary.summary_snapshot["counts"]["sent"] == 2
    assert not summary.summary_snapshot["requires_attention"]
    summary_message = flow["calls"][-1]
    assert summary_message["To"] == "owner@example.com"
    assert (
        summary_message.get_body().get_content().splitlines()
        == summary.body.splitlines()
    )
    _post(flow, "finalize_monthly_cycle")
    cycle = flow["cycle"]
    cycle.refresh_from_db()
    assert cycle.status == "ZAKLJUCEN" and cycle.completed_at is not None
    assert cycle.completion_snapshot["summary_attempt_id"] == summary.pk
    assert cycle.completion_snapshot["sent_count"] == 2
    assert cycle.completion_snapshot["failed_count"] == 0
    assert cycle.completion_snapshot["unknown_count"] == 0
    assert cycle.completion_snapshot["archive_verified_count"] == 2
    evidence = cycle.completion_snapshot.copy()
    timestamp = cycle.completed_at
    assert services.zakljuci_mesecni_cikel(cycle).completion_snapshot == evidence
    assert services.zakljuci_mesecni_cikel(cycle).completed_at == timestamp
    with pytest.raises(services.ZakljucenoObdobje):
        services.shrani_prvi_korak(PERIOD, ISSUE_DATE)
    assert flow["client"].get(reverse("ui:dashboard")).status_code == 200
    assert Invoice.objects.count() == 2 and MonthlyCycle.objects.count() == 1
    for index, invoice in enumerate(flow["invoices"]):
        invoice.refresh_from_db()
        assert invoice.status == "POSLAN"
        assert _issued_content(invoice) == flow["fiscal"][index]
        assert (
            NumberSequence.objects.get(
                company=invoice.company, tip="NP", leto=0
            ).zadnja_stevilka
            == 41 + index * 50
        )
    assert (
        list(
            MonthlyBatch.objects.order_by("company_id").values_list(
                "calculation_snapshot", flat=True
            )
        )
        == flow["snapshots"]
    )
    _assert_delivery(flow)
    _assert_backups(flow)

    # Accountant controls are a separate, following-month workflow (PRD §9).
    # Explicit as_of avoids changing clocks or mutating the finished cycle.
    for index, company in enumerate(flow["companies"]):
        result = deliver_accountant_control_report(
            company_id=company.pk, period=PERIOD, as_of=date(2030, 12, 1)
        )
        assert result.code == "ACCEPTED"
        report = AccountantReport.objects.get(company=company)
        assert [row["invoice_number"] for row in report.snapshot["issued_rows"]] == [
            flow["invoices"][index].stevilka
        ]
        assert report.snapshot["issued_rows"][0]["gross_amount"] == str(
            flow["invoices"][index].skupaj_z_ddv
        )
        assert report.snapshot["recap_rows"][0]["gross_amount"] == str(
            flow["invoices"][index].skupaj_z_ddv
        )
        message = flow["calls"][-1]
        assert message["To"] == company.email_racunovodja
        attachments = list(message.iter_attachments())
        assert len(attachments) == 1 and attachments[0].get_content_type() == "text/csv"
        assert attachments[0].get_payload(decode=True) == render_accountant_report_csv(
            report
        )
        artifact = AccountantReportPdfArtifact.objects.get(report=report)
        assert _hash(_pdf(artifact.local_path, flow["root"])) == artifact.checksum
        calls = len(flow["calls"])
        assert (
            deliver_accountant_control_report(
                company_id=company.pk, period=PERIOD, as_of=date(2030, 12, 1)
            ).code
            == "CURRENT"
        )
        assert len(flow["calls"]) == calls
    # Editing live source data cannot rewrite issued fiscal snapshots or files.
    for index, (company, lease, invoice) in enumerate(
        zip(flow["companies"], flow["leases"], flow["invoices"], strict=True)
    ):
        company.naziv = f"Changed issuer {index}"
        save_financial_fixture(company, update_fields=["naziv"])
        lease.tenant.naziv = f"Changed tenant {index}"
        save_business_record(lease.tenant, update_fields=["naziv"])
        update_financial_fixture(lease.leaseitem_set.all(), znesek_neto_mesecno=Decimal("999.00"))
        invoice.refresh_from_db()
        assert _issued_content(invoice) == flow["fiscal"][index]
        assert _hash(_rendered(flow, invoice).read_bytes()) == invoice.checksum


def test_two_company_complete_success(flow):
    _start_and_issue(flow)
    _post(flow, "send_delivery")
    assert (
        list(DeliveryAttempt.objects.values_list("result", flat=True))
        == ["ACCEPTED"] * 2
    )
    _finish(flow)


def test_two_company_confirmed_failure_safe_recovery(flow):
    _start_and_issue(flow)
    beta = flow["companies"][1]
    flow["outcomes"][beta.email_posiljatelj] = "failed"
    _post(flow, "send_delivery")
    _post(flow, "archive_sent_invoices")
    assert ArchivedFile.objects.count() == 1
    first_archive = list(ArchivedFile.objects.values())
    first_bytes = {
        path: (Path(path).read_bytes(), Path(path).stat().st_mtime_ns)
        for path in (first_archive[0]["local_path"], first_archive[0]["icloud_path"])
    }
    state = services.stanje_zakljucne_kontrole_mesecnega_cikla(flow["cycle"])
    assert state["failed_count"] == 1 and state["sent_count"] == 1
    assert {
        row["company_id"]: (row["sent_count"], row["failed_count"])
        for row in state["per_company"]
    } == {flow["companies"][0].pk: (1, 0), beta.pk: (0, 1)}
    assert state["requires_attention"] and not state["all_required_work_successful"]
    with pytest.raises(ValidationError):
        services.zakljuci_mesecni_cikel(flow["cycle"])
    _post(flow, "send_delivery")
    assert len(flow["calls"]) == 2  # Neither terminal result is automatically retried.
    failed = DeliveryAttempt.objects.get(invoice=flow["invoices"][1])
    assert failed.result == "CONFIRMED_FAILURE"
    retry = prepare_retry_attempt(failed)
    assert retry.sequence == 2
    assert (retry.pdf_path, retry.pdf_checksum) == (
        failed.pdf_path,
        failed.pdf_checksum,
    )
    flow["outcomes"].clear()
    _post(flow, "send_delivery")
    assert len(flow["calls"]) == 3
    assert list(ArchivedFile.objects.values()) == first_archive
    _finish(flow)
    for path, identity in first_bytes.items():
        assert (Path(path).read_bytes(), Path(path).stat().st_mtime_ns) == identity
    assert DeliveryAttempt.objects.filter(invoice=flow["invoices"][0]).count() == 1
    failed.refresh_from_db()
    assert failed.result == "CONFIRMED_FAILURE"


def test_unknown_requires_documented_resolution_without_second_send(flow):
    _start_and_issue(flow)
    flow["outcomes"][flow["companies"][1].email_posiljatelj] = "unknown"
    _post(flow, "send_delivery")
    unknown = DeliveryAttempt.objects.get(invoice=flow["invoices"][1])
    assert unknown.result == "UNKNOWN"
    state = services.stanje_zakljucne_kontrole_mesecnega_cikla(flow["cycle"])
    assert state["unknown_count"] == 1 and state["requires_attention"]
    for action in ("prepare_delivery", "send_delivery", "archive_sent_invoices"):
        _post(flow, action)
    assert len(flow["calls"]) == DeliveryAttempt.objects.count() == 2
    with pytest.raises(ValidationError):
        prepare_retry_attempt(unknown)
    with pytest.raises(ValidationError):
        services.zakljuci_mesecni_cikel(flow["cycle"])
    values = {"invoice_id": unknown.invoice_id, "attempt_id": unknown.pk}
    response = flow["client"].post(
        reverse("ui:mesecni_obracun"),
        {"action": "unknown_sent", **values, "reason": ""},
    )
    assert response.status_code == 302 and "unknown_error=1" in response.url
    assert not AuditLogEntry.objects.filter(
        object_id=unknown.pk, razlog="Verified in provider Sent folder"
    ).exists()
    _post(flow, "unknown_sent", **values, reason="Verified in provider Sent folder")
    assert (
        AuditLogEntry.objects.filter(
            object_id=unknown.pk, razlog="Verified in provider Sent folder"
        ).count()
        == 1
    )
    _post(flow, "send_delivery")
    assert len(flow["calls"]) == DeliveryAttempt.objects.count() == 2
    flow["outcomes"].clear()
    _finish(flow)
    unknown.refresh_from_db()
    assert unknown.result == "UNKNOWN"  # Preserve original transport evidence.
