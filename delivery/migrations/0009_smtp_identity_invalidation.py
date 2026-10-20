"""SQLite invalidation also covers bulk updates and A/B/A identity changes."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("delivery", "0008_smtpverification")]

    operations = [
        migrations.RunSQL(
            """
            CREATE TRIGGER smtp_company_identity_changed
            AFTER UPDATE OF email_posiljatelj, keychain_kljuc_posiljatelja ON companies_company
            WHEN OLD.email_posiljatelj IS NOT NEW.email_posiljatelj
              OR OLD.keychain_kljuc_posiljatelja IS NOT NEW.keychain_kljuc_posiljatelja
            BEGIN
                UPDATE delivery_smtpverification SET invalidated_at = CURRENT_TIMESTAMP
                WHERE company_id = NEW.id AND invalidated_at IS NULL;
            END;
            """,
            "DROP TRIGGER smtp_company_identity_changed;",
        ),
        migrations.RunSQL(
            """
            CREATE TRIGGER smtp_summary_identity_changed
            AFTER UPDATE OF email, keychain_credential_key ON delivery_monthlysummaryconfiguration
            WHEN OLD.email IS NOT NEW.email
              OR OLD.keychain_credential_key IS NOT NEW.keychain_credential_key
            BEGIN
                UPDATE delivery_smtpverification SET invalidated_at = CURRENT_TIMESTAMP
                WHERE company_id IS NULL AND invalidated_at IS NULL;
            END;
            """,
            "DROP TRIGGER smtp_summary_identity_changed;",
        ),
        migrations.RunSQL(
            """
            CREATE TRIGGER smtp_summary_identity_deleted
            AFTER DELETE ON delivery_monthlysummaryconfiguration
            BEGIN
                UPDATE delivery_smtpverification SET invalidated_at = CURRENT_TIMESTAMP
                WHERE company_id IS NULL AND invalidated_at IS NULL;
            END;
            """,
            "DROP TRIGGER smtp_summary_identity_deleted;",
        ),
    ]
