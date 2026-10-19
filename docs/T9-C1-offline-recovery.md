# T9-C1: Offline recovery preflight

This service inspects an isolated recovery directory and its original backup ZIP.
It never activates an installation, writes a database, changes settings, renders
PDFs, resolves delivery attempts, accesses Keychain or sends messages.

## Requirements and scope

The governing requirements are PRD v1.2 §11.2, “Arhiv: lokalno + iCloud”, and
§11.3, “Varnostna kopija: Dropbox”; SAD v1.2 §1, “Odločitev o skladu”, including
interrupted SMTP recovery; FSD v1.2 §1.1, `NumberSequence`, and the immutable
invoice rules; and the operational manual §1, “Varnostne kopije in obnova”,
particularly “Kaj je preverjeno in kaj morate pregledati dodatno” and “Ustavitev,
preklop in možnost vrnitve”.

The original ZIP is mandatory: the existing restore service does not retain its
manifest. Preflight binds every recovered file to that ZIP and checks the
database digest against its manifest. A matching checksum is an integrity check,
not proof of the ZIP's authenticity or proof that no later invoices exist.
Keep the ZIP and recovery directory quiescent during inspection.

Only the current installed model schema and exact migration inventory are
supported. No migrations run. Unknown schemas, tables, views, triggers, foreign
key declarations, primary keys, required uniqueness or migration histories fail closed. This is not
a full audit of financial calculations or every historical business snapshot.

Uniqueness validation covers every current model-declared `UniqueConstraint`,
unique field, one-to-one field and `unique_together`. It checks recovered SQLite
index metadata, indexed columns, ordering, collation and uniqueness. Named table
constraints must retain their identity and backing autoindex. Conditional indexes
must retain their name and model-declared predicate. Django compiles expected DDL
using a detached, unopened SQLite wrapper; only recovered SQLite is queried.
Indexes are never rebuilt. Equivalent predicates written differently from the
installed model DDL can be rejected conservatively.

## Service interface

Call `archive.recovery.preflight_recovery(source_zip, recovery_directory,
current_roots=..., proposed_roots=...)` after initializing Django. It returns a
JSON-serializable dictionary. Both input paths must be absolute, physical paths.
The caller supplies all current archive roots explicitly; the service never
queries the current database. The database, media and rendering locations come
from deployment settings. Current roots must describe the actual installation.

`current_roots` has exactly `local_root`, `icloud_root`, and `dropbox_root`.
Omit `proposed_roots` for inventory alone. A complete proposal has exactly:

- `database_path`: destination SQLite filename; deployment setting `MARJAN_DB_PATH`.
- `local_root`, `icloud_root`, `dropbox_root`: operational `ArchiveConfiguration` roots.
- `media_root`: deployment setting `MARJAN_MEDIA_ROOT`.
- `rendering_root`: deployment setting `MARJAN_RENDERING_ROOT`.

Preflight creates none of these destinations. All six must be separate, new
paths. Existing destinations are rejected, even empty directories. Overlaps
with current roots, recorded historical roots, the ZIP or the recovery
directory are rejected. Comparisons conservatively ignore case and normalize
Unicode. Traversal, unsafe portable names, host symlinks, Windows reparse points,
network destinations and conflicting roots are rejected.

Windows and POSIX historical paths are parsed lexically. A foreign destination
is labelled `LEXICAL_ONLY_TARGET_HOST_REQUIRED`; preflight does not claim to
check its existence, links, permissions or cloud availability on another host.
Host paths receive read-only component and existence checks. These checks do not
reserve destinations against future changes.

## Supported mapping previews

Root proposals distinguish database configuration from deployment configuration.
The backup records the historical archive roots and rendering root; it does not
record the original database or media root. These unknown origins stay unknown.

`ArchivedFile.local_path` and `.icloud_path` receive a component-aware preview
under their respective proposed roots. The suffix must agree on both sides,
match the frozen invoice identity and have the invoice's registered checksum.
No substring replacement is used. iCloud contents are never inspected offline.

An `IN_PROGRESS` entry may legitimately have no local archive file in the captured
snapshot. The inventory records `availability=MISSING_INCOMPLETE` and
`verified=false`, preserves the entry and requires manual archive reconciliation.
An existing local file must still pass SHA-256 validation. Invalid identity,
paths, invoice binding or checksum metadata always block. A `VERIFIED` entry
requires valid local bytes. A file captured in the ZIP but lost or changed after
restoration still fails the exact recovery inventory or byte check. Preflight
never publishes an archive file, marks an entry verified or retries archival.

`AccountantReportPdfArtifact.local_path` receives a runtime locator preview
under the proposed local root. Its immutable stored metadata must remain
unchanged. The suffix must identify the original report, frozen issuer and
period. A future runtime locator requires implementation in T9-C2.

Relative official PDF paths, company logos, signatory images, frozen invoice
assets and calculated monthly asset identities remain unchanged. Their
resolved destination is shown beneath the proposed rendering or media root.
Frozen assets retain their checksum-based names. Delivery PDF references must
agree with their invoice. Accountant CSV attachment identity and checksum are
checked from the stored report, without rendering a PDF or exporting contents.

Historical `BackupSnapshot.path`, monthly source asset names, audit records,
archive verification evidence and initial setup evidence are preserved.
Historical verification is never restoration approval. There is no arbitrary
path substitution, immutable snapshot rewrite, source ZIP rewrite or unsupported
absolute media/PDF mapping. eSLOG recovery is explicitly blocked in this phase.

## Report semantics

- `VERIFIED_OFFLINE` occurs only in `offline_state`: supported v2 bytes, schema
  and implemented consistency checks pass. It never grants production approval.
- `REQUIRES_MANUAL_RECONCILIATION` is the overall `state` of such a v2 snapshot.
  Numbering, delivery, attachments and target-host evidence remain mandatory.
- `BLOCKED_INTEGRITY` means a file, schema, business reference, delivery record,
  proposed destination or input cannot pass the implemented checks safely.
- `UNSUPPORTED_FORMAT` means the format cannot establish v2 completeness.
  Legacy v1 retains `Legacy v1: official PDF completeness is not verified.`

`activation_status` is always `BLOCKED`; `eligible_for_activation` is always
false. `eligible_for_controlled_activation_review` is true only when a supported
v2 snapshot has no hard blockers. It means the data can enter a later review,
not that reconciliation is complete. There is no approval or force parameter.

The inventory lists company identities; invoice identifiers, types and statuses;
NP and annual ST counters and numeric maxima; official PDF paths and SHA-256;
media and frozen asset references; archive references; backup history; monthly
cycles and batches; accountant report identifiers; delivery states and technical
references; and historical UNKNOWN resolution evidence. Reports exclude
credentials, Keychain identifiers, recipients, subjects, message bodies,
financial amounts and document contents. Treat paths and company identities as
private operational metadata and store the report accordingly.

Counters below observed numbers, duplicate or malformed numbers and inconsistent
sequence identities block inspection. Counters above observed numbers require
manual review because existing initial numbering can legitimately start above
local invoices. No counter is authoritative after restoration.

PREPARED, SUBMISSION_STARTED and UNKNOWN attempts require explicit review across
invoice, monthly summary and accountant delivery. Historical resolutions are
inventory evidence only. No attempt is transitioned or retried. Active cycles
must not resume automatically. Missing or corrupt registered PDFs always block;
registered documents are never regenerated.

## Remaining T9-C2 work

Implement a separately approved, controlled activation process only after the
reconciliation evidence and its acceptance rules are agreed. It must reconcile
all issuers and sequences with later external invoices, all delivery channels
with later evidence, every contract attachment with an external inventory, and
active monthly work with preserved snapshots. The current model has no contract
attachment registry, so a complete media tree alone cannot prove completeness.

T9-C2 must define and implement destination-host validation, preservation of the
previous installation, exclusive process shutdown, controlled publication and
rollback, operational path mapping, immutable accountant artifact lookup,
renewal of storage verification and deployment configuration. It must preserve
official bytes and all financial/delivery history, prevent concurrent production
copies and accidental retries, and record explicit approval. Perform a real
macOS restoration exercise. C1 provides none of those activation operations.
