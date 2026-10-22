"""Read-only recovery inspection. No activation, repair, ORM reads or delivery.

The original ZIP is required because restore deliberately does not retain its
manifest. Every restored byte is bound to that ZIP before business inspection.
Reports contain identifiers and technical references, never message bodies,
recipient lists, financial snapshots or credential identifiers.
"""

import hashlib
import json
import os
import re
import sqlite3
import stat
import unicodedata
from collections import Counter
from contextlib import closing
from datetime import UTC, date, datetime
from importlib import import_module
from pathlib import Path
from zipfile import ZipFile

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.backends.sqlite3.base import DatabaseWrapper
from django.db.migrations.loader import MigrationLoader
from django.db.models import UniqueConstraint

from core.pravila import slugify_naziv

from .backup import _hash_file
from .restore import (
    RestoreError,
    _entries,
    _json_object,
    _relative_parts,
    _source_roots,
    _stored_path,
    _verify_database,
    read_manifest,
    snapshot_official_pdfs,
    verify_official_pdfs,
)

ROOTS = {
    "database_path",
    "local_root",
    "icloud_root",
    "dropbox_root",
    "media_root",
    "rendering_root",
}
MANUAL = [
    "NUMBERING: Reconcile every issuer/sequence against all invoices issued after the snapshot; counters are not authoritative.",
    "DELIVERY: Reconcile invoice, summary and accountant deliveries after the snapshot, including Sent-folder evidence; never automatically retry.",
    "ATTACHMENTS: Compare all contract attachments with an external inventory; the current schema has no attachment registry.",
    "TARGET: Verify proposed paths, iCloud availability, permissions and Keychain on the target Mac with all senders stopped.",
    "ACTIVATION: A separate controlled activation and explicit manual approval are required; this report never authorizes production.",
]


def _issue(report, code, reference=""):
    report["hard_blockers"].append({"code": code, "reference": reference})


def _safe_host_path(path):
    """Reject links/reparse points in every existing component, without following."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise RestoreError("Host paths must be absolute without traversal.")
    if path.drive.startswith("\\\\"):
        raise RestoreError("Network paths are not supported offline.")
    for part in [*reversed(path.parents), path]:
        if part.exists() or part.is_symlink():
            metadata = part.lstat()
            if stat.S_ISLNK(metadata.st_mode) or getattr(
                metadata, "st_file_attributes", 0
            ) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise RestoreError("Unsafe filesystem link.")
            if part != path and not stat.S_ISDIR(metadata.st_mode):
                raise RestoreError("Non-directory path component.")
    return path


def _overlaps(left, right):
    def parts(path):
        return tuple(
            unicodedata.normalize("NFC", part).casefold() for part in path.parts
        )

    left, right = parts(left), parts(right)
    return left[: len(right)] == right or right[: len(left)] == left


def _schema(database):
    schema = list(database.execute("SELECT name, type, tbl_name, sql FROM sqlite_master"))
    migration = import_module("delivery.migrations.0009_smtp_identity_invalidation")
    # SQLite removes the final semicolon; otherwise require the exact trusted DDL.
    expected_triggers = {
        (name, "trigger", table, operation.sql.strip().removesuffix(";"))
        for (name, table), operation in zip(
            (
                ("smtp_company_identity_changed", "companies_company"),
                (
                    "smtp_summary_identity_changed",
                    "delivery_monthlysummaryconfiguration",
                ),
                (
                    "smtp_summary_identity_deleted",
                    "delivery_monthlysummaryconfiguration",
                ),
            ),
            migration.Migration.operations,
            strict=True,
        )
    }
    if {row for row in schema if row[1] not in {"table", "index"}} != expected_triggers:
        raise RestoreError("Unsupported executable database schema.")
    objects = {name: kind for name, kind, _, _ in schema}
    for model in apps.get_models():
        table = model._meta.db_table
        info = list(database.execute(f'PRAGMA table_info("{table}")'))
        columns = {row[1] for row in info}
        if objects.get(table) != "table" or columns != {
            field.column for field in model._meta.local_fields
        }:
            raise RestoreError("Unsupported recovery schema.")
        if {row[1] for row in info if row[5]} != {model._meta.pk.column}:
            raise RestoreError("Unsupported recovery primary key schema.")
        expected_foreign_keys = {
            (
                field.remote_field.model._meta.db_table,
                field.column,
                field.target_field.column,
            )
            for field in model._meta.local_fields
            if field.is_relation and field.db_constraint
        }
        actual_foreign_keys = {
            (row[2], row[3], row[4])
            for row in database.execute(f'PRAGMA foreign_key_list("{table}")')
        }
        if actual_foreign_keys != expected_foreign_keys:
            raise RestoreError("Unsupported recovery foreign key schema.")
    _unique_schema(database)
    tables = {name for name, kind in objects.items() if kind == "table"}
    if tables != {model._meta.db_table for model in apps.get_models()} | {
        "django_migrations",
        "sqlite_sequence",
    }:
        raise RestoreError("Unsupported recovery tables.")
    expected = set(MigrationLoader(None, ignore_no_migrations=True).disk_migrations)
    actual = list(database.execute("SELECT app, name FROM django_migrations"))
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise RestoreError("Unsupported recovery migrations.")


def _sql_tokens(sql):
    """Ignore DDL spacing/identifier quoting, preserving string literals."""
    tokens = re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[\w]+|[^\s]", sql)
    return tuple(
        token if token.startswith("'") else token.strip('"').casefold()
        for token in tokens
    )


def _unique_schema(database):
    # This detached wrapper only compiles expected DDL from model metadata.
    # Never enter its schema-editor context or connect to any database.
    offline = DatabaseWrapper(
        {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
        alias="recovery_schema",
    )
    editor = offline.schema_editor(collect_sql=True, atomic=False)
    for model in apps.get_models():
        table = model._meta.db_table
        indexes = list(database.execute(f'PRAGMA index_list("{table}")'))
        table_sql = database.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        table_tokens = _sql_tokens(table_sql)

        def matching(index, fields, partial):
            if index[2] != 1 or bool(index[4]) != partial:
                return False
            # Names come from recovered SQLite, so quote them as SQL identifiers.
            quoted = index[1].replace('"', '""')
            keys = [
                (row[2], row[3], (row[4] or "BINARY").casefold())
                for row in database.execute(f'PRAGMA index_xinfo("{quoted}")')
                if row[5]
            ]
            return keys == [
                (
                    field.column,
                    0,
                    (getattr(field, "db_collation", None) or "BINARY").casefold(),
                )
                for field in fields
            ]

        for constraint in model._meta.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            fields = [model._meta.get_field(name) for name in constraint.fields]
            if (
                not fields
                or constraint.expressions
                or constraint.include
                or constraint.opclasses
                or constraint.deferrable
                or constraint.nulls_distinct is not None
            ):
                raise RestoreError("Unsupported unique constraint definition.")
            if constraint.condition is not None:
                expected = constraint.create_sql(model, editor)
                actual = database.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND name=? AND tbl_name=?",
                    (constraint.name, table),
                ).fetchone()
                valid = (
                    expected is not None
                    and actual is not None
                    and _sql_tokens(str(expected)) == _sql_tokens(actual[0])
                    and any(
                        index[1] == constraint.name
                        and index[3] == "c"
                        and matching(index, fields, True)
                        for index in indexes
                    )
                )
            else:
                expected = constraint.constraint_sql(model, editor)
                tokens = _sql_tokens(str(expected)) if expected is not None else ()
                valid = (
                    bool(tokens)
                    and any(
                        table_tokens[start : start + len(tokens)] == tokens
                        for start in range(len(table_tokens) - len(tokens) + 1)
                    )
                    and any(
                        index[3] == "u" and matching(index, fields, False)
                        for index in indexes
                    )
                )
            if not valid:
                raise RestoreError(
                    f"Missing or weakened unique constraint: {table}.{constraint.name}."
                )
        implicit = [
            (field,)
            for field in model._meta.local_fields
            if field.unique and not field.primary_key
        ]
        implicit += [
            tuple(model._meta.get_field(name) for name in names)
            for names in model._meta.unique_together
        ]
        for fields in implicit:
            # AlterUniqueTogether/AlterField can use an explicitly named index
            # rather than an inline SQLite autoindex in valid Django migrations.
            expected = editor._create_unique_sql(model, fields)
            explicit = {
                row[0]: row[1]
                for row in database.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                    (table,),
                )
            }
            if not any(
                matching(index, fields, False)
                and (
                    index[3] == "u"
                    or index[3] == "c"
                    and index[1] in explicit
                    and expected is not None
                    and _sql_tokens(explicit[index[1]]) == _sql_tokens(str(expected))
                )
                for index in indexes
            ):
                raise RestoreError(f"Missing or weakened field uniqueness: {table}.")
    if offline.connection is not None:
        raise RestoreError(
            "Offline schema compilation unexpectedly opened a connection."
        )


def _rows(database, table, columns):
    # Table and column names are internal constants, never caller input.
    return [
        dict(zip(columns, row, strict=True))
        for row in database.execute(
            f'SELECT {", ".join(columns)} FROM "{table}" ORDER BY id'
        )
    ]


def _file_reference(
    report,
    work,
    *,
    reference,
    value,
    prefix,
    checksum="",
    immutable=True,
    allow_missing=False,
):
    item = {
        "reference": reference,
        "stored_path": value,
        "root": prefix,
        "immutable": immutable,
    }
    report["inventory"]["file_references"].append(item)
    try:
        parts = _relative_parts(value)
        file = _safe_host_path(work.joinpath(prefix, *parts))
        if allow_missing:
            try:
                file.lstat()
            except FileNotFoundError:
                item.update(verified=False, availability="MISSING_INCOMPLETE")
                return item
        if not file.is_file():
            raise RestoreError("Missing referenced file.")
        actual = _hash_file(file)
        item["sha256"] = actual
        if checksum and (
            not re.fullmatch(r"[0-9a-f]{64}", checksum) or actual != checksum
        ):
            raise RestoreError("Referenced file checksum differs.")
        if parts[0] == "nespremenljivi-asseti" and (
            len(parts) != 2 or actual != parts[1]
        ):
            raise RestoreError("Immutable asset checksum differs.")
        item["verified"] = True
    except (RestoreError, OSError, ValueError):
        item["verified"] = False
        _issue(report, "REFERENCED_FILE_INVALID", reference)
    return item


def _archive_relative(value, root):
    relative = _stored_path(value).relative_to(_stored_path(root)).as_posix()
    _relative_parts(relative)
    return relative


def _accountant_metadata(report, database):
    """Validate snapshot-only CSV identity without PDF rendering or live queries."""
    from accounting_export.csv_renderer import (
        accountant_report_csv_filename,
        render_accountant_report_csv,
    )
    from accounting_export.models import AccountantReport

    metadata = {}
    for row in _rows(
        database,
        "accounting_export_accountantreport",
        [
            "id",
            "company_id",
            "period",
            "schema_version",
            "snapshot",
        ],
    ):
        reference = f"AccountantReport:{row['id']}"
        try:
            snapshot = json.loads(row["snapshot"])
            if row["schema_version"] != 1 or snapshot["schema_version"] != 1:
                raise ValueError
            header = snapshot["header"]
            name = slugify_naziv(header["company_name"].strip())
            tax = header["company_tax_number"].strip()
            registration = header["company_registration_number"].strip()
            for component in (name, tax, registration):
                if len(_relative_parts(component)) != 1:
                    raise ValueError
            period = header["period"]
            if (
                not re.fullmatch(r"[0-9]{4}-[0-9]{2}", period)
                or row["period"] != period + "-01"
            ):
                raise ValueError
            instance = AccountantReport(pk=row["id"], snapshot=snapshot)
            data = render_accountant_report_csv(instance)
            metadata[row["id"]] = {
                "pdf_relative": f"Kontrolni-dokumenti/{name}-{tax}-{registration}/{period[:4]}/{period[5:]}/report-{row['id']}/{period}-{name}.pdf",
                "csv_filename": accountant_report_csv_filename(instance),
                "csv_checksum": hashlib.sha256(data).hexdigest(),
            }
        except (ValidationError, RestoreError, ValueError, TypeError, KeyError):
            _issue(report, "UNSUPPORTED_ACCOUNTANT_SNAPSHOT", reference)
    return metadata


def _numbering(report, invoices, sequences):
    maxima, seen = {}, set()
    counters = {}
    for row in sequences:
        key = (row["company_id"], row["tip"], row["leto"])
        if (
            key in counters
            or row["zadnja_stevilka"] < 0
            or not (key[1:] == ("NP", 0) or key[1] == "ST" and key[2] > 0)
        ):
            _issue(report, "INVALID_COUNTER", f"NumberSequence:{row['id']}")
        counters[key] = row["zadnja_stevilka"]
    for row in invoices:
        number = row["stevilka"]
        if not number:
            if row["status"] not in {
                "OSNUTEK",
                "ZA_PONOVNI_PREGLED",
                "ARHIVIRAN_NI_IZDAN",
            }:
                _issue(report, "MISSING_ISSUED_NUMBER", f"Invoice:{row['id']}")
            continue
        match = re.fullmatch(r"NP-([0-9]{5,})|ST-([0-9]{4})-([0-9]{4,})", number)
        if not match:
            _issue(report, "INVALID_INVOICE_NUMBER", f"Invoice:{row['id']}")
            continue
        tip = "NP" if match[1] else "ST"
        year = 0 if tip == "NP" else int(match[2])
        value = int(match[1] or match[3])
        canonical = f"NP-{value:05d}" if tip == "NP" else f"ST-{year}-{value:04d}"
        key = (row["company_id"], tip, year)
        identity = (*key, value)
        if (
            identity in seen
            or value < 1
            or canonical != number
            or tip == "ST"
            and str(year) != row["datum_izdaje"][:4]
            or row["document_type"] in {"NP", "ST"}
            and row["document_type"] != tip
        ):
            _issue(report, "INCONSISTENT_NUMBERING", f"Invoice:{row['id']}")
        seen.add(identity)
        maxima[key] = max(maxima.get(key, 0), value)
    for key in sorted(counters.keys() | maxima.keys()):
        maximum, counter = maxima.get(key, 0), counters.get(key)
        report["inventory"]["numbering"].append(
            {
                "company_id": key[0],
                "tip": key[1],
                "year": key[2],
                "snapshot_counter": counter,
                "highest_invoice_number": maximum,
                "highest_issued_number": (
                    f"NP-{maximum:05d}"
                    if key[1] == "NP"
                    else f"ST-{key[2]}-{maximum:04d}"
                )
                if maximum
                else None,
                "authoritative_for_production": False,
            }
        )
        if counter is None or counter < maximum:
            _issue(report, "COUNTER_INVOICE_MISMATCH", f"{key[0]}:{key[1]}:{key[2]}")
        elif counter > maximum:
            report["manual_requirements"].append(
                f"NUMBERING: {key[0]}:{key[1]}:{key[2]} counter exceeds snapshot invoices; verify initial numbering evidence and external invoices."
            )


def _deliveries(report, database, invoices, accountant):
    known = {row["id"]: row for row in invoices}
    latest = {}
    report["inventory"]["delivery_resolutions"] = [
        dict(zip(["id", "object_id", "resolution"], row, strict=True))
        for row in database.execute(
            "SELECT a.id, a.object_id, a.nova_vrednost FROM core_auditlogentry a "
            "JOIN django_content_type c ON c.id=a.content_type_id "
            "WHERE c.app_label='delivery' AND c.model='deliveryattempt' "
            "AND a.dejanje='SPREMEMBA' AND a.polje='unknown_resolution' "
            "AND a.stara_vrednost='UNRESOLVED' "
            "AND a.nova_vrednost IN ('SENT_VERIFIED', 'NOT_SENT_CONFIRMED') ORDER BY a.id"
        )
    ]
    resolutions = {}
    for row in report["inventory"]["delivery_resolutions"]:
        if row["object_id"] in resolutions:
            _issue(
                report,
                "CONFLICTING_DELIVERY_RESOLUTIONS",
                f"DeliveryAttempt:{row['object_id']}",
            )
        resolutions[row["object_id"]] = row["resolution"]
    for table, owner in (
        ("delivery_deliveryattempt", "invoice_id"),
        ("delivery_monthlysummaryattempt", "cycle_id"),
        ("delivery_accountantreportdeliveryattempt", "report_id"),
    ):
        columns = [
            "id",
            owner,
            "sequence",
            "result",
            "prepared_at",
            "submission_started_at",
            "completed_at",
        ]
        if owner == "invoice_id":
            columns += ["pdf_path", "pdf_checksum", "execution_id"]
        elif owner == "report_id":
            columns += ["attachment_filename", "attachment_checksum"]
        seen = set()
        for row in _rows(database, table, columns):
            reference = f"{table}:{row['id']}"
            result = row["result"]
            item = dict(row, table=table)
            report["inventory"]["delivery_attempts"].append(item)
            allowed = {
                "PREPARED",
                "SUBMISSION_STARTED",
                "ACCEPTED",
                "CONFIRMED_FAILURE",
                "UNKNOWN",
            }
            if owner == "invoice_id":
                allowed.add("CANCELLED")
                invoice = known[row[owner]]
                if (row["pdf_path"], row["pdf_checksum"]) != (
                    invoice["rendered_pdf_path"],
                    invoice["checksum"],
                ):
                    _issue(report, "DELIVERY_PDF_MISMATCH", reference)
                if row["sequence"] > latest.get(row[owner], {}).get("sequence", 0):
                    latest[row[owner]] = row
            elif owner == "report_id":
                attachment = accountant.get(row[owner])
                if not attachment or (
                    row["attachment_filename"],
                    row["attachment_checksum"],
                ) != (attachment["csv_filename"], attachment["csv_checksum"]):
                    _issue(report, "ACCOUNTANT_ATTACHMENT_MISMATCH", reference)
            identity = (row[owner], row["sequence"])
            if (
                result not in allowed
                or row["sequence"] < 1
                or identity in seen
                or result in {"ACCEPTED", "UNKNOWN"}
                and not row["submission_started_at"]
                or result in {"ACCEPTED", "UNKNOWN", "CONFIRMED_FAILURE", "CANCELLED"}
                and not row["completed_at"]
                or result in {"PREPARED", "SUBMISSION_STARTED"}
                and row["completed_at"]
                or result == "SUBMISSION_STARTED"
                and not row["submission_started_at"]
                or result == "PREPARED"
                and row["submission_started_at"]
            ):
                _issue(report, "INCONSISTENT_DELIVERY_METADATA", reference)
            try:
                timestamps = [
                    datetime.fromisoformat(row[field])
                    for field in (
                        "prepared_at",
                        "submission_started_at",
                        "completed_at",
                    )
                    if row[field]
                ]
                # Django's SQLite backend stores UTC datetimes without offsets.
                timestamps = [
                    value.replace(tzinfo=UTC) if value.utcoffset() is None else value
                    for value in timestamps
                ]
                if timestamps != sorted(timestamps):
                    raise ValueError
            except (ValueError, TypeError):
                _issue(report, "INVALID_DELIVERY_TIMESTAMPS", reference)
            seen.add(identity)
            if result in {"PREPARED", "SUBMISSION_STARTED", "UNKNOWN"}:
                report["manual_requirements"].append(
                    f"DELIVERY: Review {reference} ({result}); no retry is authorized."
                )
    for invoice in invoices:
        last = latest.get(invoice["id"])
        expected = {
            "POSLAN": {"ACCEPTED", "UNKNOWN"},
            "NAPAKA": {"CONFIRMED_FAILURE"},
            "REZULTAT_NI_ZNAN": {"UNKNOWN", "SUBMISSION_STARTED"},
        }
        if invoice["status"] in expected and (
            not last or last["result"] not in expected[invoice["status"]]
        ):
            _issue(
                report, "INVOICE_DELIVERY_STATE_MISMATCH", f"Invoice:{invoice['id']}"
            )
        if (
            last
            and last["result"] == "UNKNOWN"
            and invoice["status"] == "POSLAN"
            and resolutions.get(last["id"]) != "SENT_VERIFIED"
        ):
            _issue(report, "MISSING_UNKNOWN_RESOLUTION", f"Invoice:{invoice['id']}")
        if (
            last
            and last["result"] == "ACCEPTED"
            and invoice["status"] not in {"POSLAN", "STORNIRAN", "POPRAVLJEN"}
        ):
            _issue(
                report, "INVOICE_DELIVERY_STATE_MISMATCH", f"Invoice:{invoice['id']}"
            )


def _inventory(report, database, work, manifest):
    inventory = report["inventory"]
    inventory["companies"] = _rows(
        database, "companies_company", ["id", "naziv", "aktivno"]
    )
    columns = [
        "id",
        "company_id",
        "document_type",
        "stevilka",
        "datum_izdaje",
        "status",
        "rendered_pdf_path",
        "checksum",
        "rendered_eslog_path",
        "rendered_eslog_checksum",
        "izdajatelj_logotip_slika_pot",
        "izdajatelj_podpisnik_slika_pot",
        "izdajatelj_naziv",
        "izdajatelj_davcna_stevilka",
    ]
    invoices = _rows(database, "invoicing_invoice", columns)
    inventory["invoices"] = [{key: row[key] for key in columns[:6]} for row in invoices]
    inventory["invoice_counts"] = [
        {"company_id": key[0], "document_type": key[1], "count": count}
        for key, count in sorted(
            Counter((r["company_id"], r["document_type"]) for r in invoices).items()
        )
    ]
    _numbering(
        report,
        invoices,
        _rows(
            database,
            "companies_numbersequence",
            ["id", "company_id", "tip", "leto", "zadnja_stevilka"],
        ),
    )
    inventory["numbering_verifications"] = _rows(
        database,
        "companies_numberingverification",
        [
            "id",
            "company_id",
            "verified_at",
            "np_last",
            "st_last",
            "st_year",
        ],
    )
    known = {row["id"]: row for row in invoices}
    accountant = _accountant_metadata(report, database)
    for row in invoices:
        if row["document_type"] not in {"NP", "ST", "STORNO", "DOBROPIS"}:
            _issue(report, "UNSUPPORTED_DOCUMENT_TYPE", f"Invoice:{row['id']}")
        if row["status"] not in {
            "OSNUTEK",
            "ZA_PONOVNI_PREGLED",
            "IZBRAN_ZA_POSILJANJE",
            "POSLAN",
            "NAPAKA",
            "REZULTAT_NI_ZNAN",
            "ARHIVIRAN_NI_IZDAN",
            "STORNIRAN",
            "POPRAVLJEN",
        }:
            _issue(report, "UNSUPPORTED_INVOICE_STATUS", f"Invoice:{row['id']}")
        for field in ("izdajatelj_logotip_slika_pot", "izdajatelj_podpisnik_slika_pot"):
            if row[field]:
                _file_reference(
                    report,
                    work,
                    reference=f"Invoice:{row['id']}.{field}",
                    value=row[field],
                    prefix="media",
                    immutable=bool(row["stevilka"]),
                )
        if row["rendered_eslog_path"] or row["rendered_eslog_checksum"]:
            _issue(report, "UNSUPPORTED_ESLOG_RECOVERY", f"Invoice:{row['id']}")
        if row["stevilka"] and not row["rendered_pdf_path"]:
            report["manual_requirements"].append(
                f"PDF: Invoice:{row['id']} has no registered PDF; inspect interrupted issuance without regenerating registered documents."
            )
    for table, field, checksum in (
        ("companies_company", "logotip", "logotip_checksum"),
        ("companies_signatory", "podpis_slika", "podpis_checksum"),
    ):
        for row in _rows(database, table, ["id", field, checksum]):
            if row[field]:
                _file_reference(
                    report,
                    work,
                    reference=f"{table}:{row['id']}.{field}",
                    value=row[field],
                    checksum=row[checksum],
                    prefix="media",
                    immutable=False,
                )
    for table, columns in (
        (
            "archive_archivedfile",
            [
                "id",
                "invoice_id",
                "artifact_type",
                "local_path",
                "icloud_path",
                "checksum",
                "status",
            ],
        ),
        (
            "accounting_export_accountantreportpdfartifact",
            ["id", "report_id", "local_path", "checksum"],
        ),
    ):
        for row in _rows(database, table, columns):
            reference = f"{table}:{row['id']}"
            inventory["archive_references"].append(dict(row, table=table))
            try:
                relative = _archive_relative(
                    row["local_path"], manifest["source_roots"]["local_root"]
                )
                if "icloud_path" in row:
                    if (
                        _archive_relative(
                            row["icloud_path"], manifest["source_roots"]["icloud_root"]
                        )
                        != relative
                    ):
                        raise RestoreError("Archive roots disagree.")
                    if row["status"] not in {"VERIFIED", "IN_PROGRESS"}:
                        raise RestoreError("Unsupported archive status.")
                    invoice = known[row["invoice_id"]]
                    if (
                        row["artifact_type"] != "PDF"
                        or row["checksum"] != invoice["checksum"]
                    ):
                        raise RestoreError("Archive artifact differs from invoice.")
                    issued = date.fromisoformat(invoice["datum_izdaje"])
                    # Same frozen identity layout as archive.services.pdf_relative_path,
                    # without importing rendering/SMTP dependencies into inspection.
                    expected = (
                        f"Arhiv/{slugify_naziv(invoice['izdajatelj_naziv'])}-{invoice['izdajatelj_davcna_stevilka']}/"
                        f"{issued.year}/{issued.month:02d}/{invoice['stevilka']}.pdf"
                    )
                    if (
                        len(
                            _relative_parts(
                                f"{slugify_naziv(invoice['izdajatelj_naziv'])}-{invoice['izdajatelj_davcna_stevilka']}"
                            )
                        )
                        != 1
                    ):
                        raise RestoreError("Unsafe archive issuer identity.")
                    if relative != expected:
                        raise RestoreError(
                            "Archive path differs from invoice identity."
                        )
                    if row["status"] == "IN_PROGRESS":
                        report["manual_requirements"].append(
                            f"ARCHIVE: Reconcile incomplete {reference}; iCloud is not verified offline."
                        )
                elif (
                    row["report_id"] not in accountant
                    or relative != accountant[row["report_id"]]["pdf_relative"]
                ):
                    raise RestoreError(
                        "Accountant PDF path differs from report identity."
                    )
                if not re.fullmatch(r"[0-9a-f]{64}", row["checksum"]):
                    raise RestoreError("Missing archive checksum.")
                _file_reference(
                    report,
                    work,
                    reference=reference,
                    value=relative,
                    prefix="archive",
                    checksum=row["checksum"],
                    allow_missing=row.get("status") == "IN_PROGRESS",
                )
            except (RestoreError, ValueError):
                _issue(report, "INVALID_ARCHIVE_REFERENCE", reference)
    inventory["cycles"] = _rows(
        database,
        "invoicing_monthlycycle",
        ["id", "obdobje", "datum_izdaje", "status", "trenutni_korak", "completed_at"],
    )
    for row in inventory["cycles"]:
        if row["status"] == "V_TEKU":
            report["manual_requirements"].append(
                f"CYCLE: Reconcile active MonthlyCycle:{row['id']}; do not resume automatically."
            )
        if (
            row["status"] not in {"V_TEKU", "ZAKLJUCEN"}
            or not 1 <= row["trenutni_korak"] <= 6
            or (row["status"] == "ZAKLJUCEN") != bool(row["completed_at"])
        ):
            _issue(report, "INCONSISTENT_MONTHLY_CYCLE", f"MonthlyCycle:{row['id']}")
    inventory["batches"] = _rows(
        database,
        "invoicing_monthlybatch",
        [
            "id",
            "cycle_id",
            "company_id",
            "preflight_status",
            "calculation_status",
            "calculation_schema_version",
        ],
    )
    for row in _rows(
        database,
        "invoicing_monthlybatch",
        [
            "id",
            "calculation_status",
            "calculation_snapshot",
            "calculation_schema_version",
        ],
    ):
        snapshot = json.loads(row["calculation_snapshot"])
        if not isinstance(snapshot, dict):
            _issue(report, "INVALID_MONTHLY_SNAPSHOT", f"MonthlyBatch:{row['id']}")
            continue
        company = snapshot.get("company")
        if row["calculation_status"] == "CALCULATED":
            if (
                row["calculation_schema_version"] not in {3, 4, 5}
                or snapshot.get("schema_version") != row["calculation_schema_version"]
            ):
                _issue(
                    report, "UNSUPPORTED_MONTHLY_SNAPSHOT", f"MonthlyBatch:{row['id']}"
                )
            if not isinstance(company, dict) or not isinstance(
                company.get("signatory"), dict
            ):
                _issue(
                    report,
                    "INVALID_MONTHLY_ASSET_REFERENCES",
                    f"MonthlyBatch:{row['id']}",
                )
                continue
            for owner, checksum_field, source_field in (
                (company, "logo_checksum", "logo_source"),
                (company["signatory"], "signature_checksum", "signature_source"),
            ):
                checksum = owner.get(checksum_field)
                if not isinstance(checksum, str) or not re.fullmatch(
                    r"[0-9a-f]{64}", checksum
                ):
                    _issue(
                        report,
                        "INVALID_MONTHLY_ASSET_CHECKSUM",
                        f"MonthlyBatch:{row['id']}.{checksum_field}",
                    )
                    continue
                _file_reference(
                    report,
                    work,
                    reference=f"MonthlyBatch:{row['id']}.{checksum_field}",
                    value=f"nespremenljivi-asseti/{checksum}",
                    prefix="media",
                    checksum=checksum,
                )
                # Source names are historical evidence; future issuance uses the
                # immutable checksum reference, not a rewritten snapshot name.
                if owner.get(source_field):
                    inventory["historical_asset_sources"].append(
                        {
                            "reference": f"MonthlyBatch:{row['id']}.{source_field}",
                            "stored_path": owner[source_field],
                            "action": "PRESERVE_SNAPSHOT",
                        }
                    )
    inventory["backup_history"] = _rows(
        database,
        "archive_backupsnapshot",
        [
            "id",
            "path",
            "created_at",
            "verified_at",
            "trigger",
            "zip_checksum",
            "database_checksum",
        ],
    )
    for row in inventory["backup_history"]:
        try:
            _stored_path(row["path"])
        except RestoreError:
            _issue(
                report, "INVALID_HISTORICAL_BACKUP_PATH", f"BackupSnapshot:{row['id']}"
            )
    inventory["setup_evidence"] = {
        "archive_verification": "Historical only; must be renewed on target host.",
        "initial_setup_completion": "Preserve original evidence; never reuse it as restoration approval.",
        "sample_previews": "Disposable previews are not registered official PDFs and are not captured from RENDERING_ROOT.",
        "audit_records": database.execute(
            "SELECT count(*) FROM core_auditlogentry"
        ).fetchone()[0],
    }
    inventory["accountant_reports"] = _rows(
        database,
        "accounting_export_accountantreport",
        [
            "id",
            "company_id",
            "period",
            "schema_version",
            "generated_at",
        ],
    )
    _deliveries(report, database, invoices, accountant)


def _mapping_preview(report, proposed, protected, work, source, manifest):
    if not proposed:
        report["manual_requirements"].append(
            "PATHS: Supply all six proposed installation roots for a complete preview."
        )
        return
    if set(proposed) != ROOTS:
        raise RestoreError("Mapping must specify exactly six installation roots.")
    roots = {key: _stored_path(str(value)) for key, value in proposed.items()}
    for root in roots.values():
        if root.drive.startswith("\\\\"):
            raise RestoreError("Network destinations are not supported offline.")
        _relative_parts("/".join(root.parts[1:]))
    # Conservative collision rules cover default case-insensitive macOS volumes.
    normalized = {
        key: tuple(
            unicodedata.normalize("NFC", part).casefold() for part in value.parts
        )
        for key, value in roots.items()
    }
    for key, parts in normalized.items():
        for other, other_parts in normalized.items():
            if key != other and parts[: len(other_parts)] == other_parts:
                raise RestoreError("Overlapping proposed destinations.")
    historical = {**manifest["source_roots"]}
    if manifest["format_version"] == 2:
        historical["rendering_root"] = manifest["rendering_root"]
    for key, root in roots.items():
        for old in [*historical.values(), *map(str, protected), str(work), str(source)]:
            previous = _stored_path(old)
            if type(root) is type(previous) and _overlaps(root, previous):
                raise RestoreError("Destination overlaps protected or historical data.")
        host_checked = Path(str(root)).is_absolute()
        if host_checked:
            path = _safe_host_path(str(root))
            if path.exists():
                raise RestoreError("Proposed destination already exists.")
        report["path_mappings"].append(
            {
                "reference": key,
                "origin": "database"
                if key in manifest["source_roots"]
                else "deployment",
                "historical": historical.get(key),
                "proposed": str(root),
                "validation": "HOST_PATH_CHECKED"
                if host_checked
                else "LEXICAL_ONLY_TARGET_HOST_REQUIRED",
            }
        )
    for row in report["inventory"]["archive_references"]:
        for field, root_name in (
            ("local_path", "local_root"),
            ("icloud_path", "icloud_root"),
        ):
            if field not in row:
                continue
            relative = _archive_relative(
                row[field], manifest["source_roots"][root_name]
            )
            report["path_mappings"].append(
                {
                    "reference": f"{row['table']}:{row['id']}.{field}",
                    "origin": "database",
                    "historical": row[field],
                    "proposed": str(
                        roots[root_name].joinpath(*_relative_parts(relative))
                    ),
                    "action": "PRESERVE_IMMUTABLE_METADATA_USE_RUNTIME_LOCATOR"
                    if "report_id" in row
                    else "PREVIEW_ARCHIVE_REFERENCE",
                }
            )
    for item in report["inventory"]["file_references"]:
        if item["root"] not in {"media", "rendering"}:
            continue
        report["path_mappings"].append(
            {
                "reference": item["reference"],
                "origin": "database",
                "historical": item["stored_path"],
                "proposed": str(
                    roots[item["root"] + "_root"].joinpath(
                        *_relative_parts(item["stored_path"])
                    )
                ),
                "action": "PRESERVE_RELATIVE_REFERENCE",
                "immutable": item["immutable"],
            }
        )
    report["warnings"].append(
        "Destinations are preview only. Foreign paths receive lexical checks only; target-host permissions and availability are unverified."
    )


def preflight_recovery(
    source_zip, recovery_directory, *, current_roots, proposed_roots=None
):
    """Inspect an existing isolated restore; return a JSON-serializable report.

    current_roots must explicitly contain the current local/iCloud/Dropbox roots.
    Deployment paths come from settings, without querying the live database.
    No reconciliation approval/force parameter exists. Production stays blocked.
    """
    report = {
        "state": "BLOCKED_INTEGRITY",
        "offline_state": "BLOCKED_INTEGRITY",
        "activation_status": "BLOCKED",
        "eligible_for_activation": False,
        "eligible_for_controlled_activation_review": False,
        "verified_checks": [],
        "warnings": [],
        "hard_blockers": [],
        "manual_requirements": list(MANUAL),
        "path_mappings": [],
        "inventory": {
            "numbering": [],
            "file_references": [],
            "archive_references": [],
            "delivery_attempts": [],
            "historical_asset_sources": [],
        },
    }
    try:
        if set(current_roots) != {"local_root", "icloud_root", "dropbox_root"}:
            raise RestoreError("All current archive roots are required.")
        protected = [
            _safe_host_path(value)
            for value in [
                *current_roots.values(),
                settings.MEDIA_ROOT,
                settings.RENDERING_ROOT,
                settings.DATABASES["default"]["NAME"],
            ]
        ]
        work = _safe_host_path(recovery_directory)
        source = _safe_host_path(source_zip)
        if not work.is_dir() or not source.is_file() or _overlaps(work, source):
            raise RestoreError("Invalid isolated recovery inputs.")
        if any(_overlaps(work, root) for root in protected):
            raise RestoreError("Recovery overlaps current installation.")
        # Walk before reading SQLite or hashing files, including unused files.
        actual = set()
        for parent, directories, files in os.walk(work, followlinks=False):
            for name in directories + files:
                entry = _safe_host_path(Path(parent) / name)
                relative = entry.relative_to(work).as_posix()
                _relative_parts(relative)
                if entry.is_file():
                    if not stat.S_ISREG(entry.lstat().st_mode):
                        raise RestoreError("Special recovery file.")
                    actual.add(relative)
                elif not entry.is_dir():
                    raise RestoreError("Special recovery entry.")
        zip_checksum = _hash_file(source)
        with ZipFile(source) as archive:
            if archive.getinfo("manifest.json").file_size > 16 * 1024 * 1024:
                raise RestoreError("Manifest is too large.")
            raw = json.loads(
                archive.read("manifest.json"), object_pairs_hook=_json_object
            )
            if (
                isinstance(raw, dict)
                and type(raw.get("format_version")) is int
                and raw["format_version"] not in {1, 2}
            ):
                report["state"] = report["offline_state"] = "UNSUPPORTED_FORMAT"
                report["warnings"].append(
                    "Unsupported backup format; no completeness or activation claim is possible."
                )
                return report
            manifest = read_manifest(archive)
            report["snapshot"] = {
                "created_at": manifest["created_at"],
                "format_version": manifest["format_version"],
                "zip_sha256": zip_checksum,
                "database_sha256": manifest["database_checksum"],
            }
            if manifest["format_version"] == 1:
                report["warnings"].append(
                    "Legacy v1: official PDF completeness is not verified."
                )
            for root in _source_roots(manifest["source_roots"]):
                if Path(str(root)).is_absolute() and _overlaps(work, Path(str(root))):
                    raise RestoreError("Recovery overlaps historical installation.")
            if manifest.get("rendering_root"):
                root = _stored_path(manifest["rendering_root"])
                if Path(str(root)).is_absolute() and _overlaps(work, Path(str(root))):
                    raise RestoreError("Recovery overlaps historical rendering root.")
            entries = _entries(archive)
            if archive.testzip() is not None:
                raise RestoreError("ZIP CRC verification failed.")
            expected = {
                entry.filename
                for entry, _ in entries
                if not entry.is_dir() and entry.filename != "manifest.json"
            }
            for name in sorted(expected - actual):
                _issue(report, "MISSING_RECOVERY_FILE", name)
            for name in sorted(actual - expected):
                _issue(report, "UNEXPECTED_RECOVERY_FILE", name)
            for name in sorted(expected & actual):
                digest = hashlib.sha256()
                with archive.open(name) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                if _hash_file(work / name) != digest.hexdigest():
                    _issue(report, "RECOVERY_BYTES_DIFFER", name)
            database_path = work / "database/db.sqlite3"
            if _hash_file(database_path) != manifest["database_checksum"]:
                raise RestoreError("Database checksum differs from manifest.")
            report["verified_checks"].extend(["ZIP_CRC", "DATABASE_SHA256"])
            if not report["hard_blockers"]:
                report["verified_checks"].extend(
                    ["EXACT_RECOVERY_FILE_INVENTORY", "RESTORED_BYTES_MATCH_ZIP"]
                )
            _verify_database(work, work, manifest["source_roots"])
            with closing(
                sqlite3.connect(
                    database_path.as_uri() + "?mode=ro&immutable=1", uri=True
                )
            ) as database:
                database.execute("PRAGMA trusted_schema=OFF")
                database.execute("PRAGMA query_only=ON")
                if database.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise RestoreError("SQLite integrity check failed.")
                _schema(database)
                report["verified_checks"].extend(
                    [
                        "SQLITE_INTEGRITY",
                        "SQLITE_FOREIGN_KEYS",
                        "CURRENT_SCHEMA_AND_MIGRATIONS",
                    ]
                )
                report["inventory"]["historical_configuration"] = manifest[
                    "source_roots"
                ]
                report["inventory"]["deployment_references"] = {
                    "MARJAN_DB_PATH": "not recorded in backup",
                    "MARJAN_MEDIA_ROOT": "not recorded in backup",
                    "MARJAN_RENDERING_ROOT": manifest.get("rendering_root"),
                    "credentials": "not included; provision separately on target host",
                }
                _inventory(report, database, work, manifest)
                if manifest["format_version"] == 2:
                    verify_official_pdfs(archive, manifest, database_path)
                    report["inventory"]["official_pdfs"] = snapshot_official_pdfs(
                        database_path
                    )
                    for item in report["inventory"]["official_pdfs"]:
                        _file_reference(
                            report,
                            work,
                            reference=f"Invoice:{item['invoice_id']}.rendered_pdf_path",
                            value=item["path"],
                            prefix="rendering",
                            checksum=item["checksum"],
                        )
                    report["verified_checks"].append(
                        "ZIP_OFFICIAL_PDF_PATHS_AND_SHA256"
                    )
            _mapping_preview(report, proposed_roots, protected, work, source, manifest)
            if (
                _hash_file(database_path) != manifest["database_checksum"]
                or _hash_file(source) != zip_checksum
            ):
                raise RestoreError("Recovery inputs changed during inspection.")
            if manifest["format_version"] == 1:
                report["state"] = report["offline_state"] = "UNSUPPORTED_FORMAT"
            elif not report["hard_blockers"]:
                report["offline_state"] = "VERIFIED_OFFLINE"
                report["state"] = "REQUIRES_MANUAL_RECONCILIATION"
                report["eligible_for_controlled_activation_review"] = True
    except RestoreError as error:
        # Only internally authored diagnostics; never echo SQLite/untrusted values.
        _issue(report, "PREFLIGHT_REJECTED", str(error))
    except Exception:  # noqa: BLE001 -- untrusted snapshots fail closed, no secret-bearing exception text
        _issue(report, "PREFLIGHT_FAILED", "Unable to safely inspect recovery inputs.")
    if report["hard_blockers"]:
        report["state"] = report["offline_state"] = "BLOCKED_INTEGRITY"
    return report
