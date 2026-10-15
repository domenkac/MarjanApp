# Persistent data paths

M1 adds explicit deployment configuration. It does not install or launch MarjanApp.

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
database before normal startup. M1 does not initialize a production database.
Keep these paths stable across restarts and source updates. Existing archive
and Dropbox locations remain configured through `ArchiveConfiguration`.

SQLite `IMMEDIATE` transactions and archive, backup, and restore safeguards are
unchanged, consistent with `03_SAD_v1.2.md`, sections 1 (Odločitev o skladu)
and 7 (Presek s specifičnimi PRD zahtevami).
