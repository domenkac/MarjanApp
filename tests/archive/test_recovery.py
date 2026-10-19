"""Offline preflight against synthetic snapshots; no production operations."""
# ruff: noqa: F401, F811 -- imported pytest fixtures

import hashlib
import json
import re
import shutil
import sqlite3
import stat
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from django.db import connection, models

from archive.backup import create_backup_snapshot
from archive.models import ArchivedFile, BackupSnapshot
from archive.recovery import preflight_recovery
from companies.models import NumberSequence
from delivery.models import DeliveryAttempt, MonthlySummaryAttempt
from invoicing.models import Invoice, MonthlyCycle
from tests.accounting_export.test_csv_renderer import report
from tests.archive.test_backup import locations
from tests.archive.test_official_pdf_backup import official_pdfs
from tests.archive.test_restore import recover, rewrite

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def no_external_operations(monkeypatch):
    import smtplib
    import socket

    import keyring

    def forbidden(*args, **kwargs):
        pytest.fail("Offline recovery must not use network, SMTP or Keychain")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    monkeypatch.setattr(smtplib, "SMTP_SSL", forbidden)
    monkeypatch.setattr(keyring, "get_password", forbidden)
    monkeypatch.setattr(keyring, "set_password", forbidden)


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def recovery(official_pdfs, locations, tmp_path, settings):
    for kind, year in (("NP", 0), ("ST", 2026)):
        NumberSequence.objects.create(
            company=official_pdfs[0].company, tip=kind, leto=year, zadnja_stevilka=1
        )
    asset = b"frozen logo"
    relative = f"nespremenljivi-asseti/{sha(asset)}"
    (settings.MEDIA_ROOT / relative).parent.mkdir()
    (settings.MEDIA_ROOT / relative).write_bytes(asset)
    models.QuerySet.update(
        Invoice.objects.filter(pk=official_pdfs[0].pk),
        izdajatelj_logotip_slika_pot=relative,
    )
    # Include archive metadata to exercise both roots and preview rows.
    invoice = official_pdfs[0]
    data = (settings.RENDERING_ROOT / invoice.rendered_pdf_path).read_bytes()
    from archive.services import pdf_relative_path

    relative_archive = pdf_relative_path(invoice)
    local = locations[0] / relative_archive
    local.parent.mkdir(parents=True)
    local.write_bytes(data)
    ArchivedFile.objects.create(
        invoice=invoice,
        artifact_type="PDF",
        status="VERIFIED",
        local_path=str(local),
        icloud_path=str(locations[1] / relative_archive),
        checksum=invoice.checksum,
    )
    created = create_backup_snapshot(trigger="MANUAL")
    assert created.success, created.message
    snapshot = Path(created.snapshot.path), locations
    target = tmp_path / "recovered"
    restored = recover(snapshot, target)
    assert restored.success, restored.message
    return {
        "source": snapshot[0],
        "target": target,
        "current": dict(
            zip(
                ("local_root", "icloud_root", "dropbox_root"),
                locations[:3],
                strict=True,
            )
        ),
        "invoices": official_pdfs,
    }


def inspect(recovery, **kwargs):
    return preflight_recovery(
        recovery["source"],
        recovery["target"],
        current_roots=recovery["current"],
        **kwargs,
    )


def blockers(report):
    return {row["code"] for row in report["hard_blockers"]}


def rebind(recovery, tmp_path, *, sql=None, manifest_edit=None, extra=()):
    """Simulate a verified but semantically inconsistent source snapshot."""
    database_path = recovery["target"] / "database/db.sqlite3"
    if sql:
        with sqlite3.connect(database_path) as database:
            database.executescript(sql)
    database_bytes = database_path.read_bytes()

    def replace(name, content):
        if name == "database/db.sqlite3":
            return database_bytes
        if name == "manifest.json":
            manifest = json.loads(content)
            manifest["database_checksum"] = sha(database_bytes)
            if manifest_edit:
                manifest_edit(manifest)
            return json.dumps(manifest).encode()
        return content

    recovery["source"] = rewrite(
        (recovery["source"], []), tmp_path, replace=replace, extra=extra
    )


def proposed(tmp_path):
    return {
        "database_path": str(tmp_path / "new-db" / "db.sqlite3"),
        **{
            key: str(tmp_path / ("new-" + key))
            for key in (
                "local_root",
                "icloud_root",
                "dropbox_root",
                "media_root",
                "rendering_root",
            )
        },
    }


def tree_bytes(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_valid_v2_is_verified_offline_but_production_is_blocked(recovery, tmp_path):
    before = tree_bytes(recovery["target"])
    report = inspect(recovery, proposed_roots=proposed(tmp_path))
    assert report["offline_state"] == "VERIFIED_OFFLINE", report["hard_blockers"]
    assert report["state"] == "REQUIRES_MANUAL_RECONCILIATION"
    assert report["activation_status"] == "BLOCKED"
    assert report["eligible_for_activation"] is False
    assert report["eligible_for_controlled_activation_review"] is True
    assert not report["hard_blockers"]
    assert len(report["inventory"]["numbering"]) == 2
    assert {
        row["highest_issued_number"] for row in report["inventory"]["numbering"]
    } == {"NP-00001", "ST-2026-0001"}
    assert all(
        row["snapshot_counter"] == row["highest_invoice_number"] == 1
        and not row["authoritative_for_production"]
        for row in report["inventory"]["numbering"]
    )
    assert report["snapshot"]["format_version"] == 2
    assert {row["reference"] for row in report["path_mappings"]} >= {
        "database_path",
        "local_root",
        "icloud_root",
        "dropbox_root",
        "media_root",
        "rendering_root",
    }
    assert tree_bytes(recovery["target"]) == before
    for invoice in recovery["invoices"]:
        name = "rendering/" + invoice.rendered_pdf_path
        with ZipFile(recovery["source"]) as archive:
            assert before[name] == archive.read(name)
        assert sha(before[name]) == invoice.checksum


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_registered_pdf_loss_or_corruption_blocks(recovery, damage):
    path = recovery["target"] / "rendering" / recovery["invoices"][0].rendered_pdf_path
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupted original")
    report = inspect(recovery)
    assert report["state"] == "BLOCKED_INTEGRITY"
    assert "REFERENCED_FILE_INVALID" in blockers(report)
    assert any(
        "rendered_pdf_path" in row["reference"] for row in report["hard_blockers"]
    )
    assert not report["eligible_for_controlled_activation_review"]
    assert (
        not path.exists()
        if damage == "missing"
        else path.read_bytes() == b"corrupted original"
    )


@pytest.mark.parametrize(
    "path", ["company-999/NP-00001.pdf", "../private.pdf", "C:/private.pdf"]
)
def test_invalid_official_pdf_identity(recovery, tmp_path, path):
    rebind(
        recovery,
        tmp_path / "altered",
        sql=f"UPDATE invoicing_invoice SET rendered_pdf_path='{path}' WHERE document_type='NP';",
    )
    report = inspect(recovery)
    assert report["state"] == "BLOCKED_INTEGRITY"


def test_old_windows_roots_preview_on_macos(recovery, tmp_path):
    historical = {key: f"C:\\OldMac\\{key}" for key in recovery["current"]}
    with sqlite3.connect(recovery["target"] / "database/db.sqlite3") as database:
        for field, root in historical.items():
            database.execute(
                f"UPDATE archive_archiveconfiguration SET {field}=?", (root,)
            )
        row = database.execute(
            "SELECT id, invoice_id FROM archive_archivedfile"
        ).fetchone()
        invoice = recovery["invoices"][0]
        from archive.services import pdf_relative_path

        suffix = str(pdf_relative_path(invoice)).replace("/", "\\")
        database.execute(
            "UPDATE archive_archivedfile SET local_path=?, icloud_path=? WHERE id=?",
            (
                historical["local_root"] + "\\" + suffix,
                historical["icloud_root"] + "\\" + suffix,
                row[0],
            ),
        )
    rebind(
        recovery,
        tmp_path / "windows",
        manifest_edit=lambda m: m.update(
            source_roots=historical, rendering_root="C:\\OldMac\\rendered"
        ),
    )
    targets = {
        "database_path": "/Users/recovery/Marjan/database/db.sqlite3",
        **{
            key: "/Users/recovery/Marjan/" + key
            for key in proposed(tmp_path)
            if key != "database_path"
        },
    }
    report = inspect(recovery, proposed_roots=targets)
    assert report["offline_state"] == "VERIFIED_OFFLINE", report
    row = next(
        row
        for row in report["path_mappings"]
        if row["reference"].endswith(".local_path")
    )
    assert row["historical"].startswith("C:\\OldMac")
    assert row["proposed"].startswith("/Users/recovery/Marjan/local_root/Arhiv/")
    if Path().anchor != "/":
        assert (
            report["path_mappings"][0]["validation"]
            == "LEXICAL_ONLY_TARGET_HOST_REQUIRED"
        )
    assert not Path(targets["database_path"]).is_file()


@pytest.mark.parametrize(
    "unsafe",
    ["../escape", "/new/../escape", "C:relative", "/", "/new/CON", "/new/bad:stream"],
)
def test_unsafe_mapping(recovery, tmp_path, unsafe):
    targets = proposed(tmp_path)
    targets["media_root"] = unsafe
    assert inspect(recovery, proposed_roots=targets)["state"] == "BLOCKED_INTEGRITY"


@pytest.mark.parametrize(
    "overlap", ["archive", "case", "source", "live", "recovery", "existing"]
)
def test_conflicting_mapping(recovery, tmp_path, overlap):
    targets = proposed(tmp_path)
    if overlap == "archive":
        targets["database_path"] = targets["local_root"] + "/db.sqlite3"
    elif overlap == "case":
        targets["media_root"] = targets["local_root"].upper()
    else:
        targets["media_root"] = {
            "source": str(recovery["source"]),
            "live": str(recovery["current"]["local_root"]),
            "recovery": str(recovery["target"]),
            "existing": str(tmp_path),
        }[overlap]
    report = inspect(recovery, proposed_roots=targets)
    assert report["state"] == "BLOCKED_INTEGRITY"


@pytest.mark.parametrize("kind", ["media", "immutable"])
def test_missing_media_or_frozen_asset(recovery, kind):
    root = recovery["target"] / "media"
    path = (
        next((root / "nespremenljivi-asseti").iterdir())
        if kind == "immutable"
        else root / "signature.png"
    )
    path.unlink()
    report = inspect(recovery)
    assert report["state"] == "BLOCKED_INTEGRITY"
    assert "MISSING_RECOVERY_FILE" in blockers(report)


def test_counter_behind_invoices_is_not_repaired(recovery, tmp_path):
    rebind(
        recovery,
        tmp_path / "numbering",
        sql="UPDATE companies_numbersequence SET zadnja_stevilka=0 WHERE tip='NP';",
    )
    before = tree_bytes(recovery["target"])
    report = inspect(recovery)
    assert "COUNTER_INVOICE_MISMATCH" in blockers(report)
    assert tree_bytes(recovery["target"]) == before


def test_initial_counter_above_invoices_requires_manual_review(recovery, tmp_path):
    rebind(
        recovery,
        tmp_path / "numbering",
        sql="UPDATE companies_numbersequence SET zadnja_stevilka=20 WHERE tip='NP';",
    )
    report = inspect(recovery)
    assert report["state"] == "REQUIRES_MANUAL_RECONCILIATION", report
    assert any(
        "initial numbering evidence" in row for row in report["manual_requirements"]
    )


@pytest.mark.parametrize("result", ["PREPARED", "SUBMISSION_STARTED", "UNKNOWN"])
def test_unresolved_delivery_never_resends(recovery, tmp_path, result, monkeypatch):
    invoice = recovery["invoices"][0]
    started = "'2026-09-01T10:00:00+00:00'" if result != "PREPARED" else "NULL"
    completed = "'2026-09-01T10:01:00+00:00'" if result == "UNKNOWN" else "NULL"
    rebind(
        recovery,
        tmp_path / "delivery",
        sql=f"""
        INSERT INTO delivery_deliveryattempt
        (invoice_id, sequence, prepared_at, submission_started_at, completed_at,
         sender, recipients_to, recipients_cc, recipients_bcc, pdf_path, pdf_checksum, result, result_code, message)
        VALUES ({invoice.pk}, 1, '2026-09-01T09:00:00+00:00', {started}, {completed},
         'private@example.com', '["private@example.com"]', '[]', '[]', '{invoice.rendered_pdf_path}',
         '{invoice.checksum}', '{result}', '', 'password=TOP_SECRET');
    """,
    )
    import delivery.services

    monkeypatch.setattr(
        delivery.services, "send_pdf_email", lambda *a, **k: pytest.fail("No sends")
    )
    monkeypatch.setattr(
        delivery.services,
        "recover_interrupted_submissions",
        lambda *a, **k: pytest.fail("No transitions"),
    )
    before = tree_bytes(recovery["target"])
    report = inspect(recovery)
    assert report["state"] == "REQUIRES_MANUAL_RECONCILIATION", report
    assert any(
        f"({result})" in requirement for requirement in report["manual_requirements"]
    )
    assert "TOP_SECRET" not in json.dumps(report)
    assert "private@example.com" not in json.dumps(report)
    assert tree_bytes(recovery["target"]) == before


def test_inconsistent_delivery_metadata_blocks(recovery, tmp_path):
    rebind(
        recovery,
        tmp_path / "delivery",
        sql="UPDATE invoicing_invoice SET status='POSLAN' WHERE document_type='NP';",
    )
    assert "INVOICE_DELIVERY_STATE_MISMATCH" in blockers(inspect(recovery))


def test_active_cycle_requires_manual_review(recovery, tmp_path):
    rebind(
        recovery,
        tmp_path / "cycle",
        sql="""
        INSERT INTO invoicing_monthlycycle (obdobje, datum_izdaje, trenutni_korak, status, created_at, updated_at)
        VALUES ('2026-09-01', '2026-09-01', 3, 'V_TEKU', '2026-09-01', '2026-09-01');
    """,
    )
    report = inspect(recovery)
    assert report["state"] == "REQUIRES_MANUAL_RECONCILIATION"
    assert any("active MonthlyCycle" in row for row in report["manual_requirements"])


def test_old_snapshot_always_requires_external_reconciliation(recovery, tmp_path):
    rebind(
        recovery,
        tmp_path / "old",
        manifest_edit=lambda m: m.update(created_at="2020-01-01T00:00:00+00:00"),
    )
    report = inspect(recovery)
    assert report["activation_status"] == "BLOCKED"
    assert any("after the snapshot" in row for row in report["manual_requirements"])
    assert report["eligible_for_activation"] is False


def test_legacy_v1_never_claims_official_completeness(recovery, tmp_path):
    def legacy(name, content):
        if name == "manifest.json":
            manifest = json.loads(content)
            manifest["format_version"] = 1
            manifest.pop("official_pdfs")
            manifest.pop("rendering_root")
            return json.dumps(manifest).encode()
        return content

    with ZipFile(recovery["source"]) as archive:
        omit = [name for name in archive.namelist() if name.startswith("rendering/")]
    recovery["source"] = rewrite(
        (recovery["source"], []), tmp_path / "legacy", replace=legacy, omit=omit
    )
    shutil.rmtree(recovery["target"] / "rendering")
    report = inspect(recovery)
    assert report["state"] == "UNSUPPORTED_FORMAT", report
    assert "Legacy v1: official PDF completeness is not verified." in report["warnings"]
    assert not report["eligible_for_controlled_activation_review"]


def test_unknown_format_is_explicit(recovery, tmp_path):
    rebind(
        recovery,
        tmp_path / "future",
        manifest_edit=lambda m: m.update(format_version=99),
    )
    assert inspect(recovery)["state"] == "UNSUPPORTED_FORMAT"


@pytest.mark.parametrize(
    "damage", ["schema", "migration", "foreign_key", "database_bytes", "extra_file"]
)
def test_database_and_inventory_fail_closed(recovery, tmp_path, damage):
    statements = {
        "schema": "ALTER TABLE invoicing_invoice ADD COLUMN unexpected TEXT;",
        "migration": "DELETE FROM django_migrations WHERE id=(SELECT max(id) FROM django_migrations);",
        "foreign_key": "PRAGMA foreign_keys=OFF; UPDATE invoicing_invoice SET company_id=99999;",
    }
    if damage in statements:
        rebind(recovery, tmp_path / damage, sql=statements[damage])
    elif damage == "database_bytes":
        (recovery["target"] / "database/db.sqlite3").write_bytes(b"corrupt sqlite")
    else:
        (recovery["target"] / "unapproved-file").write_bytes(b"unexpected")
    assert inspect(recovery)["state"] == "BLOCKED_INTEGRITY"


@pytest.mark.parametrize("target", ["recovery", "destination"])
def test_symlink_rejected(recovery, tmp_path, target):
    link = tmp_path / "unsafe-link"
    try:
        link.symlink_to(recovery["target"], target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit symlink creation")
    if target == "recovery":
        recovery["target"] = link
        report = inspect(recovery)
    else:
        targets = proposed(tmp_path)
        targets["media_root"] = str(link / "new-media")
        report = inspect(recovery, proposed_roots=targets)
    assert report["state"] == "BLOCKED_INTEGRITY"


def test_no_live_sql_no_current_changes_no_activation(recovery, settings, tmp_path):
    live_database = Path(connection.settings_dict["NAME"])
    before_live = live_database.read_bytes()
    before_recovered = tree_bytes(recovery["target"])
    before_zip = recovery["source"].read_bytes()
    deployment = (
        settings.DATABASES["default"]["NAME"],
        settings.MEDIA_ROOT,
        settings.RENDERING_ROOT,
    )
    targets = proposed(tmp_path)
    with connection.execute_wrapper(lambda *a: pytest.fail("No live SQL allowed")):
        report = inspect(recovery, proposed_roots=targets)
    assert report["offline_state"] == "VERIFIED_OFFLINE", report
    assert live_database.read_bytes() == before_live
    assert tree_bytes(recovery["target"]) == before_recovered
    assert recovery["source"].read_bytes() == before_zip
    assert deployment == (
        settings.DATABASES["default"]["NAME"],
        settings.MEDIA_ROOT,
        settings.RENDERING_ROOT,
    )
    assert all(not Path(value).exists() for value in targets.values())


@pytest.mark.parametrize(
    "damage", ["none", "missing", "wrong_report_path", "wrong_checksum"]
)
def test_accountant_immutable_artifact(recovery, tmp_path, report, damage):
    from accounting_export.archive import accountant_report_pdf_relative_path
    from accounting_export.models import AccountantReport

    company = recovery["invoices"][0].company_id
    with sqlite3.connect(recovery["target"] / "database/db.sqlite3") as database:
        database.execute(
            "INSERT INTO accounting_export_accountantreport (id, company_id, period, generated_at, schema_version, snapshot) VALUES (1, ?, '2026-07-01', '2026-08-02', 1, ?)",
            (company, json.dumps(report.snapshot)),
        )
        identity = AccountantReport(pk=1, snapshot=report.snapshot)
        relative = accountant_report_pdf_relative_path(identity).as_posix()
        stored_relative = (
            relative.replace("report-1", "report-999")
            if damage == "wrong_report_path"
            else relative
        )
        local = str(recovery["current"]["local_root"] / stored_relative)
        checksum = (
            "0" * 64
            if damage == "wrong_checksum"
            else sha(b"%PDF immutable accountant")
        )
        database.execute(
            "INSERT INTO accounting_export_accountantreportpdfartifact (report_id, local_path, checksum, verified_at, created_at) VALUES (1, ?, ?, '2026-08-02', '2026-08-02')",
            (local, checksum),
        )
    path = recovery["target"] / "archive" / stored_relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"%PDF immutable accountant")
    rebind(
        recovery,
        tmp_path / "accountant",
        extra=[("archive/" + stored_relative, path.read_bytes())],
    )
    if damage == "missing":
        path.unlink()
    before = tree_bytes(recovery["target"])
    result = inspect(recovery, proposed_roots=proposed(tmp_path))
    if damage == "none":
        assert result["state"] == "REQUIRES_MANUAL_RECONCILIATION", result
        assert any(
            row.get("action") == "PRESERVE_IMMUTABLE_METADATA_USE_RUNTIME_LOCATOR"
            for row in result["path_mappings"]
        )
    else:
        assert result["state"] == "BLOCKED_INTEGRITY", result
    assert tree_bytes(recovery["target"]) == before


def test_summary_unknown_is_reported_without_body(recovery, tmp_path):
    rebind(
        recovery,
        tmp_path / "summary",
        sql="""
        INSERT INTO invoicing_monthlycycle (id, obdobje, datum_izdaje, trenutni_korak, status, created_at, updated_at)
        VALUES (1, '2026-09-01', '2026-09-01', 6, 'V_TEKU', '2026-09-01', '2026-09-01');
        INSERT INTO delivery_monthlysummaryattempt
        (cycle_id, sequence, prepared_at, submission_started_at, completed_at, sender, recipient,
         subject, body, summary_snapshot, result, result_code, message)
        VALUES (1, 1, '2026-09-01 09:00:00', '2026-09-01 09:01:00', '2026-09-01 09:02:00',
         'secret@example.com', 'secret@example.com', 'secret subject', 'secret body', '{}', 'UNKNOWN', '', 'secret message');
    """,
    )
    result = inspect(recovery)
    assert result["state"] == "REQUIRES_MANUAL_RECONCILIATION", result
    assert any(
        "delivery_monthlysummaryattempt" in row for row in result["manual_requirements"]
    )
    assert "secret" not in json.dumps(result)


def test_monthly_frozen_asset_reference_is_mandatory(recovery, tmp_path):
    snapshot = {
        "schema_version": 5,
        "company": {
            "logo_source": "historical-logo.png",
            "logo_checksum": "1" * 64,
            "signatory": {
                "signature_source": "historical-sign.png",
                "signature_checksum": "2" * 64,
            },
        },
    }
    rebind(
        recovery,
        tmp_path / "monthly",
        sql="""
        INSERT INTO invoicing_monthlycycle (id, obdobje, datum_izdaje, trenutni_korak, status, created_at, updated_at)
        VALUES (1, '2026-09-01', '2026-09-01', 3, 'V_TEKU', '2026-09-01', '2026-09-01');
    """,
    )
    with sqlite3.connect(recovery["target"] / "database/db.sqlite3") as database:
        database.execute(
            """
            INSERT INTO invoicing_monthlybatch
            (cycle_id, company_id, preflight_status, preflight_issues, calculation_status,
             calculation_snapshot, calculation_schema_version)
            VALUES (1, ?, 'PASSED', '[]', 'CALCULATED', ?, 5)
        """,
            (recovery["invoices"][0].company_id, json.dumps(snapshot)),
        )
    rebind(recovery, tmp_path / "monthly-rebound")
    result = inspect(recovery)
    assert "REFERENCED_FILE_INVALID" in blockers(result), result
    assert any("MonthlyBatch" in row["reference"] for row in result["hard_blockers"])
    assert len(result["inventory"]["historical_asset_sources"]) == 2


def test_reparse_point_is_rejected_without_symlink_privilege(tmp_path, monkeypatch):
    from archive.recovery import _safe_host_path
    from archive.restore import RestoreError

    path = tmp_path / "reparse"
    path.mkdir()
    original = Path.lstat
    monkeypatch.setattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024, raising=False)
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda value: (
            SimpleNamespace(
                st_mode=stat.S_IFDIR,
                st_file_attributes=1024,
            )
            if value == path
            else original(value)
        ),
    )
    with pytest.raises(RestoreError, match="Unsafe filesystem link"):
        _safe_host_path(path)


@pytest.mark.parametrize(
    "status,local_state,blocked",
    [
        ("VERIFIED", "missing", True),
        ("VERIFIED", "corrupt", True),
        ("IN_PROGRESS", "missing", False),
        ("IN_PROGRESS", "correct", False),
        ("IN_PROGRESS", "corrupt", True),
    ],
)
def test_archive_publication_state(recovery, tmp_path, status, local_state, blocked):
    archived = ArchivedFile.objects.get()
    if status == "IN_PROGRESS":
        # Capture genuine incomplete work using the unchanged T9-B services.
        ArchivedFile.objects.filter(pk=archived.pk).update(status=status)
        local = Path(archived.local_path)
        if local_state == "missing":
            local.unlink()
        elif local_state == "corrupt":
            local.write_bytes(b"corrupted archive copy")
        snapshot = create_backup_snapshot(trigger="MANUAL")
        assert snapshot.success, snapshot.message
        recovery["source"] = Path(snapshot.snapshot.path)
        recovery["target"] = tmp_path / "incomplete-recovery"
        restored = recover(
            (recovery["source"], list(recovery["current"].values())), recovery["target"]
        )
        assert restored.success, restored.message
    else:
        relative = Path(archived.local_path).relative_to(
            recovery["current"]["local_root"]
        )
        local = recovery["target"] / "archive" / relative
        if local_state == "missing":
            local.unlink()
        else:
            local.write_bytes(b"corrupted archive copy")
    before = tree_bytes(recovery["target"])
    result = inspect(recovery)
    assert result["state"] == (
        "BLOCKED_INTEGRITY" if blocked else "REQUIRES_MANUAL_RECONCILIATION"
    ), result
    if status == "IN_PROGRESS":
        assert result["inventory"]["archive_references"][0]["status"] == status
        assert any(
            "Reconcile incomplete" in value for value in result["manual_requirements"]
        )
        if local_state != "corrupt":
            reference = next(
                value
                for value in result["inventory"]["file_references"]
                if value["root"] == "archive"
            )
            assert reference["verified"] is (local_state == "correct")
    assert tree_bytes(recovery["target"]) == before


def remove_inline_unique(database, table, name=None, *, field=None):
    """Remove a table constraint only in an isolated test snapshot."""
    original = database.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]
    if field:
        changed, count = re.subn(
            r'("' + re.escape(field) + r'"\s+[^,]*?)\s+UNIQUE\b', r"\1", original
        )
    else:
        changed, count = re.subn(
            r',\s*CONSTRAINT "' + re.escape(name) + r'" UNIQUE \([^)]*\)', "", original
        )
    assert count == 1
    indexes = [
        row[0]
        for row in database.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (table,),
        )
    ]
    temporary = table + "_test_rebuild"
    database.execute("PRAGMA foreign_keys=OFF")
    database.execute(
        changed.replace(f'CREATE TABLE "{table}"', f'CREATE TABLE "{temporary}"', 1)
    )
    database.execute(f'INSERT INTO "{temporary}" SELECT * FROM "{table}"')
    database.execute(f'DROP TABLE "{table}"')
    database.execute(f'ALTER TABLE "{temporary}" RENAME TO "{table}"')
    for sql in indexes:
        database.execute(sql)


@pytest.mark.parametrize(
    "table,name",
    [
        ("companies_numbersequence", "uniq_number_sequence_company_tip_leto"),
        ("invoicing_invoice", "uniq_invoice_company_stevilka"),
        ("delivery_deliveryattempt", "uniq_delivery_attempt_invoice_sequence"),
        ("delivery_monthlysummaryattempt", "uniq_monthly_summary_cycle_sequence"),
        (
            "delivery_accountantreportdeliveryattempt",
            "uniq_accountant_delivery_sequence",
        ),
        ("archive_archivedfile", "uniq_archived_file_invoice_artifact"),
    ],
)
def test_missing_required_inline_uniqueness(recovery, tmp_path, table, name):
    with sqlite3.connect(recovery["target"] / "database/db.sqlite3") as database:
        remove_inline_unique(database, table, name)
    rebind(recovery, tmp_path / "missing-unique")
    before = tree_bytes(recovery["target"])
    with connection.execute_wrapper(lambda *args: pytest.fail("No live SQL")):
        result = inspect(recovery)
    assert result["state"] == "BLOCKED_INTEGRITY", result
    assert any(name in issue["reference"] for issue in result["hard_blockers"]), result[
        "hard_blockers"
    ]
    assert tree_bytes(recovery["target"]) == before


@pytest.mark.parametrize(
    "damage",
    ["missing", "nonunique", "columns", "predicate", "renamed", "collation", "order"],
)
def test_partial_unique_index_identity(recovery, tmp_path, damage):
    name = "uniq_accepted_monthly_summary_cycle"
    sql = {
        "missing": None,
        "nonunique": f"CREATE INDEX \"{name}\" ON delivery_monthlysummaryattempt (cycle_id) WHERE result = 'ACCEPTED'",
        "columns": f"CREATE UNIQUE INDEX \"{name}\" ON delivery_monthlysummaryattempt (sequence) WHERE result = 'ACCEPTED'",
        "predicate": f"CREATE UNIQUE INDEX \"{name}\" ON delivery_monthlysummaryattempt (cycle_id) WHERE result = 'UNKNOWN'",
        "renamed": "CREATE UNIQUE INDEX renamed_unique ON delivery_monthlysummaryattempt (cycle_id) WHERE result = 'ACCEPTED'",
        "collation": f"CREATE UNIQUE INDEX \"{name}\" ON delivery_monthlysummaryattempt (cycle_id COLLATE NOCASE) WHERE result = 'ACCEPTED'",
        "order": f"CREATE UNIQUE INDEX \"{name}\" ON delivery_monthlysummaryattempt (cycle_id DESC) WHERE result = 'ACCEPTED'",
    }[damage]
    with sqlite3.connect(recovery["target"] / "database/db.sqlite3") as database:
        database.execute(f'DROP INDEX "{name}"')
        if sql:
            database.execute(sql)
    rebind(recovery, tmp_path / "partial-unique")
    before = tree_bytes(recovery["target"])
    result = inspect(recovery)
    assert result["state"] == "BLOCKED_INTEGRITY", result
    assert any(name in issue["reference"] for issue in result["hard_blockers"]), result[
        "hard_blockers"
    ]
    assert tree_bytes(recovery["target"]) == before


@pytest.mark.parametrize(
    "name",
    [
        "uniq_active_company_davcna_stevilka",
        "uniq_current_signatory_per_company",
        "uniq_active_monthly_cycle",
        "uniq_accepted_accountant_delivery",
        "uniq_active_indexation_item_date",
    ],
)
def test_other_critical_partial_uniqueness(recovery, tmp_path, name):
    with sqlite3.connect(recovery["target"] / "database/db.sqlite3") as database:
        database.execute(f'DROP INDEX "{name}"')
    rebind(recovery, tmp_path / "other-unique")
    before = tree_bytes(recovery["target"])
    result = inspect(recovery)
    assert result["state"] == "BLOCKED_INTEGRITY", result
    assert any(name in issue["reference"] for issue in result["hard_blockers"]), result[
        "hard_blockers"
    ]
    assert tree_bytes(recovery["target"]) == before


@pytest.mark.parametrize(
    "table,field",
    [
        ("accounting_export_accountantreportpdfartifact", "report_id"),
        ("catalogs_vatregime", "sifra"),
    ],
)
def test_missing_model_field_uniqueness(recovery, tmp_path, table, field):
    with sqlite3.connect(recovery["target"] / "database/db.sqlite3") as database:
        remove_inline_unique(database, table, field=field)
    rebind(recovery, tmp_path / "field-unique")
    before = tree_bytes(recovery["target"])
    result = inspect(recovery)
    assert result["state"] == "BLOCKED_INTEGRITY", result
    assert any(
        "field uniqueness: " + table in issue["reference"]
        for issue in result["hard_blockers"]
    ), result["hard_blockers"]
    assert tree_bytes(recovery["target"]) == before


@pytest.mark.parametrize(
    "damage", ["outside", "traversal", "identity", "invoice", "checksum"]
)
def test_incomplete_archive_metadata_still_fails_closed(recovery, tmp_path, damage):
    archived = ArchivedFile.objects.get()
    Path(archived.local_path).unlink()
    updates = {"status": "IN_PROGRESS"}
    updates.update(
        {
            "outside": {"local_path": str(tmp_path / "outside.pdf")},
            "traversal": {
                "local_path": str(recovery["current"]["local_root"]) + "/../escape.pdf"
            },
            "identity": {
                "local_path": archived.local_path.replace(
                    "NP-00001.pdf", "NP-99999.pdf"
                ),
                "icloud_path": archived.icloud_path.replace(
                    "NP-00001.pdf", "NP-99999.pdf"
                ),
            },
            "invoice": {"invoice_id": recovery["invoices"][1].pk},
            "checksum": {"checksum": "invalid"},
        }[damage]
    )
    ArchivedFile.objects.filter(pk=archived.pk).update(**updates)
    snapshot = create_backup_snapshot(trigger="MANUAL")
    assert snapshot.success, snapshot.message
    recovery["source"] = Path(snapshot.snapshot.path)
    recovery["target"] = tmp_path / "bad-metadata-recovery"
    restored = recover(
        (recovery["source"], list(recovery["current"].values())), recovery["target"]
    )
    assert restored.success, restored.message
    before = tree_bytes(recovery["target"])
    result = inspect(recovery)
    assert result["state"] == "BLOCKED_INTEGRITY", result
    assert "INVALID_ARCHIVE_REFERENCE" in blockers(result)
    assert tree_bytes(recovery["target"]) == before


def test_schema_compilation_opens_no_django_database(recovery, monkeypatch):
    from django.db.backends.sqlite3.base import DatabaseWrapper

    def forbidden(*args, **kwargs):
        pytest.fail("Schema inspection must not open a Django database connection")

    monkeypatch.setattr(DatabaseWrapper, "get_new_connection", forbidden)
    with connection.execute_wrapper(lambda *args: pytest.fail("No live SQL")):
        result = inspect(recovery)
    assert result["offline_state"] == "VERIFIED_OFFLINE", result["hard_blockers"]
