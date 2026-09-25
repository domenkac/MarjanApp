from datetime import date

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_confirmed_failure_migration_preserves_audit_data_and_reverses():
    executor = MigrationExecutor(connection)
    latest_targets = executor.loader.graph.leaf_nodes()
    old_targets = [
        (app, "0001_initial") if app == "delivery" else (app, migration)
        for app, migration in latest_targets
    ]
    new_targets = latest_targets

    try:
        executor.migrate(old_targets)
        old_apps = executor.loader.project_state(old_targets).apps
        Company = old_apps.get_model("companies", "Company")
        Invoice = old_apps.get_model("invoicing", "Invoice")
        DeliveryAttempt = old_apps.get_model("delivery", "DeliveryAttempt")

        company = Company.objects.create(
            naziv="Alfa d.o.o.",
            davcna_stevilka="15012557",
            maticna_stevilka="1234567000",
            naslov="Glavna 1",
            trr="SI56191000000123438",
            logotip="logo.png",
            email_racunovodja="accountant@example.com",
            email_posiljatelj="sender@example.com",
            keychain_kljuc_posiljatelja="keychain-reference",
        )
        invoice = Invoice.objects.create(
            company=company,
            document_type="NP",
            datum_izdaje=date(2026, 9, 1),
            obdobje_od=date(2026, 8, 1),
            obdobje_do=date(2026, 8, 31),
        )
        attempt = DeliveryAttempt.objects.create(
            invoice=invoice,
            sequence=3,
            sender="sender@example.com",
            recipients_to=["tenant@example.com"],
            recipients_cc=["manager@example.com"],
            recipients_bcc=["accountant@example.com"],
            pdf_path="company-1/NP-00001.pdf",
            pdf_checksum="a" * 64,
            result="FAILED_BEFORE_SUBMISSION",
            result_code="SMTP_REJECTED",
            message="550 Message rejected",
        )
        audit_data = {
            "pk": attempt.pk,
            "invoice_id": invoice.pk,
            "sequence": 3,
            "sender": "sender@example.com",
            "recipients_to": ["tenant@example.com"],
            "recipients_cc": ["manager@example.com"],
            "recipients_bcc": ["accountant@example.com"],
            "pdf_path": "company-1/NP-00001.pdf",
            "pdf_checksum": "a" * 64,
            "result_code": "SMTP_REJECTED",
            "message": "550 Message rejected",
        }

        executor = MigrationExecutor(connection)
        executor.migrate(new_targets)
        new_apps = executor.loader.project_state(new_targets).apps
        migrated = new_apps.get_model("delivery", "DeliveryAttempt").objects.get(
            pk=attempt.pk
        )
        assert migrated.result == "CONFIRMED_FAILURE"
        assert {field: getattr(migrated, field) for field in audit_data} == audit_data

        executor = MigrationExecutor(connection)
        executor.migrate(old_targets)
        old_apps = executor.loader.project_state(old_targets).apps
        restored = old_apps.get_model("delivery", "DeliveryAttempt").objects.get(
            pk=attempt.pk
        )
        assert restored.result == "FAILED_BEFORE_SUBMISSION"
        assert {field: getattr(restored, field) for field in audit_data} == audit_data
    finally:
        MigrationExecutor(connection).migrate(latest_targets)
