# Persistent data paths

M1 adds explicit deployment configuration. It does not install or launch MarjanApp.

For production browser operation use [T12-B8a local macOS installation](local-browser-macos.md).
That launcher requires a private explicit configuration, stable Django key,
existing-only SQLite and DEBUG=False. The defaults below remain development and
offline-tool behavior; they are not the production startup path. Desktop TEST
launchers and bundles are not part of this release path.

- `MARJAN_DB_PATH`: absolute path to an existing SQLite database file.
- `MARJAN_RENDERING_ROOT`: absolute directory path for rendered documents.
- `MARJAN_MEDIA_ROOT`: existing setting for uploaded images and attachments.

The two new variables accept `~/` and resolve the expanded path, including
symlinks. Empty values, relative paths, and SQLite URI or memory names are
rejected. `MARJAN_MEDIA_ROOT` retains its existing `expanduser().resolve()`
behavior. No setting creates directories or moves, copies, or opens data files.
Rendering retains its existing behavior of creating directories when writing.

Without these variables, development defaults remain `BASE_DIR/db.sqlite3`,
`BASE_DIR/rendered`, and `BASE_DIR/media`. The existing test database default
also remains unchanged.

With `MARJAN_DB_PATH` set, SQLite uses `mode=rw` for the configured live file on
every connection. A missing or inaccessible file fails when the database
connection is opened, without creating a replacement or falling back to the
development database. Settings can still load for offline restore operations.
Database settings retain a filesystem path for existing backup, restore, and
lock safeguards. Django can still create a separate test database.

The intended future macOS parent directory is
`~/Library/Application Support/MarjanApp/`. It is not a default. Deployment must
explicitly configure all three paths and separately provision or recover the
database before normal startup. M1 does not initialize a production database;
the explicit M3 procedure below provisions a new installation only.
Keep these paths stable across restarts and source updates. Existing archive
and Dropbox locations remain configured through `ArchiveConfiguration`.

SQLite `IMMEDIATE` transactions and archive, backup, and restore safeguards are
unchanged, consistent with `03_SAD_v1.2.md`, sections 1 (Odločitev o skladu)
and 7 (Presek s specifičnimi PRD zahtevami).

## First installation: a brand-new database only

Stop application processes before installation. This procedure is for a new,
empty installation with no business database to transfer. It is not an upgrade,
restore, reset, or production activation procedure. Never run `migrate` against
an existing database as part of this procedure.

After installing the dependencies in the project virtual environment, use the
following commands in a macOS terminal, from the repository root:

```sh
source .venv/bin/activate
install -d -m 700 "$HOME/Library/Application Support/MarjanApp"
export MARJAN_DB_PATH="$HOME/Library/Application Support/MarjanApp/marjan.sqlite3"
export MARJAN_MEDIA_ROOT="$HOME/Library/Application Support/MarjanApp/media"
export MARJAN_RENDERING_ROOT="$HOME/Library/Application Support/MarjanApp/rendered"
python -m app.provision_database --check-paths
```

Before authorizing creation, inspect all three printed absolute paths. Confirm
that they belong to this new installation, remain outside the source checkout,
and refer to the intended local disk. Keep the database, uploads, and rendered
documents in separate locations. The database parent must already exist. On
macOS it must belong to the current user and must not be writable by other users
or groups. Do not place the live database in iCloud or Dropbox. The intended
parent above is an operator-selected example, never a development default.

Provisioning requires an explicitly configured absolute `MARJAN_DB_PATH` (`~/`
is expanded). It rejects repository paths, dot components, repeated separators,
symlinks in any component, Windows junctions/reparse points, UNC/device paths,
and noncanonical path aliases. Use the actual canonical path if a system path
such as `/var` is a symlink. Existing targets are rejected even when empty,
corrupt, or partially initialized. Existing SQLite `-journal`, `-wal`, and `-shm`
sidecars also block provisioning. The preview creates nothing and never opens
an existing SQLite database. The storage paths are displayed using ordinary
settings; the operator must check them. Preview does not test cloud storage.

Only after reviewing the paths, explicitly authorize a new installation:

```sh
python -m app.provision_database --confirm-new-install
```

Success requires exit status zero and the message
`New installation database provisioned and verified:` followed by the reviewed
path and migration count. The command applies the existing migrations to a
private, new in-memory SQLite database with `IMMEDIATE` transactions. It verifies
the complete migration graph, Django system checks (warnings also fail), SQLite
integrity, foreign key enforcement, and foreign key consistency. It uses
[SQLite serialization](https://www.sqlite.org/c3ref/serialize.html) to write that
database into an exclusively created temporary file in the pinned parent,
flushes it to disk, rereads and verifies the stored bytes, then publishes it with
an exclusive hard link. A concurrent creator wins without being overwritten.
The final name never exposes a partially written database. The filesystem must
support hard links; there is no copy/replace fallback. On POSIX the parent is
also flushed before success. The temporary file has mode `0600` on macOS.

Parent directories are opened without following links. POSIX operations use
pinned directory descriptors; Windows holds directory handles that deny rename
and deletion. Provisioning never opens a supplied file through SQLite, never
creates application storage directories, and never calls SMTP or Keychain.
Only migration metadata is seeded. No company, lease, invoice, numbering,
readiness, or audit evidence is fabricated.

On failure, a nonzero exit status and `Provisioning failed:` explain the cause.
Do not launch the application. Fix the path, permissions, dependencies, or
filesystem support first. The command removes only its own temporary file after
ordinary errors. A crash can leave a `.marjan-provision-*.sqlite3` artifact for
diagnosis. An error after publication can leave a complete target, but is still
reported as failure. Retain any existing target for diagnosis; provisioning
never deletes or reuses it. Choose another unused target for a new attempt and
review the paths again. Repeated invocation on a successful target fails safely.

After success, retain the same explicit environment for every application run:

```sh
python manage.py check --database default
python manage.py showmigrations --plan
python -m app.local_server --config "$HOME/Library/Application Support/MarjanApp/config/server.json" --check
python -m app.local_server --config "$HOME/Library/Application Support/MarjanApp/config/server.json"
```

All migration entries must be marked `[X]`. Open `http://127.0.0.1:8000/`.
The first-run wizard remains incomplete and issuance remains blocked until its
existing checks pass (PRD v1.2 §16.4; SAD v1.2 §6.4). Saved wizard progress can be
resumed after closing the browser. Provisioning does not complete onboarding.
Prepare the private production JSON and stable key first, using the linked B8a
procedure. The production launcher does not provision or migrate; it retains
M1's `mode=rw` factory and cannot create a missing configured database.

### Verification on the production MacBook

Before deployment, repeat preview, provisioning, repeat-invocation rejection,
and missing-database startup checks in an isolated directory on the target Mac.
Verify canonical APFS paths, permissions (`0600` database/private parent), hard
links, directory flushing, symlink rejection, and the installed Python/SQLite
serialization support. Confirm all migrations and the empty wizard, browser
restart/resumption, loopback binding, and real PDF rendering. Power-loss
durability is not established by automated Windows tests; see
[Apple's fsync documentation](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/fsync.2.html)
for flushing limits. Verify cloud storage,
Keychain, SMTP, real five-company onboarding, and backup/restore separately under
the existing operational procedures. These are not performed by provisioning.
