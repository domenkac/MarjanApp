# Production local browser installation — T12-B8a

Target handoff: 2026-10-29. This procedure is prepared on Windows; native Mac
installation, launchd, Keychain, PDF, reboot and power-loss acceptance are NOT TESTED.
Independent review and the operator's explicit activation approval are required.

This is the release path defined by PRD v1.2 §13 “Tehnični okvir” and SAD v1.2
§§1–2: one logged-in Mac user, Python venv, Django 5.2, SQLite and a normal browser
at **http://127.0.0.1:8000/**. There is no login/account system, remote access,
container, native app bundle, or TEST profile in this path. Existing desktop TEST
files remain a separate experiment and are not used here.

## A–E. Reviewed source, Python, dependencies and native PDF

Use a dedicated terminal session. Do not run as root. Prepare the reviewed source
checkout at `~/MarjanApp` yourself; no command below pulls, resets, cleans, applies
patches, or changes branches. Obtain the exact approved GitHub commit SHA from the
independent approval record. Fill the placeholder; do not use an unreviewed HEAD.

```sh
set -eu
umask 077
test "$(id -u)" -ne 0
cd "$HOME/MarjanApp"
APP_SOURCE="$PWD"
EXPECTED_APPROVED_COMMIT='OPERATOR_FILL_40_CHARACTER_APPROVED_GITHUB_COMMIT_SHA'
printf '%s\n' "$EXPECTED_APPROVED_COMMIT" | grep -Eq '^[0-9a-f]{40}$' || exit 1
test "$(git rev-parse HEAD)" = "$EXPECTED_APPROVED_COMMIT" || exit 1
git diff --quiet --ignore-submodules=none || exit 1
git diff --cached --quiet --ignore-submodules=none || exit 1
APP_EVIDENCE=$(mktemp -d "$HOME/MarjanApp-production-evidence.XXXXXX")
git ls-files --others --directory -z -- . ':(top,exclude)db_before_manual_test.sqlite3' ':(top,exclude)nespremenljivi-asseti' ':(top,exclude)slike' > "$APP_EVIDENCE/untracked-source-paths.zlist" || exit 1
if [ -s "$APP_EVIDENCE/untracked-source-paths.zlist" ]; then
  printf '%s\n' 'STOP: unreviewed source checkout entries; use a separate approved checkout without changing local data.' >&2
  exit 1
fi
git status --short --branch
export PYTHONDONTWRITEBYTECODE=1
uname -m
python3 --version
python3 -c 'import platform,sys; assert platform.machine() == "arm64"; assert sys.version_info >= (3,12); print(sys.executable)'
xcode-select -p
brew --prefix
git rev-parse HEAD > "$APP_EVIDENCE/source-head.txt"
git status --short --branch > "$APP_EVIDENCE/source-status.txt"
sw_vers > "$APP_EVIDENCE/macos.txt"
APP_RUNTIME="$HOME/Library/Application Support/MarjanApp-runtime"
APP_VENV="$APP_RUNTIME/venv-$EXPECTED_APPROVED_COMMIT"
test ! -e "$APP_VENV" && test ! -L "$APP_VENV" || exit 1
install -d -m 700 "$APP_RUNTIME"
python3 -m venv "$APP_VENV"
"$APP_VENV/bin/python" -m pip install -r requirements-server.txt
"$APP_VENV/bin/python" -m pip check
"$APP_VENV/bin/python" -m pip freeze > "$APP_EVIDENCE/python-versions.txt"
brew install pango harfbuzz fontconfig glib
APP_NATIVE_LIBS="$(brew --prefix)/lib"
export DYLD_FALLBACK_LIBRARY_PATH="$APP_NATIVE_LIBS"
brew list --versions > "$APP_EVIDENCE/native-versions.txt"
"$APP_VENV/bin/python" -m weasyprint --info
"$APP_VENV/bin/python" - <<'PY'
from io import BytesIO
from weasyprint import HTML
buffer = BytesIO()
HTML(string='<html lang="sl"><p>č Č š Š ž Ž — lokalni PDF</p></html>').write_pdf(buffer)
assert buffer.getvalue().startswith(b'%PDF-')
print('Native PDF generation completed; inspect an application sample before acceptance.')
PY
```

Use native arm64 Python 3.12+, not Apple's system Python or Rosetta. The venv is
outside Git. Waitress 3.0.2 and WhiteNoise 6.12.0 are pinned; existing base
requirements and Homebrew versions are not fully locked. Retain and review the
resolved versions before activation. Do not upgrade a failing dependency silently.
WeasyPrint's native libraries must be available both in this shell and to launchd.
Missing fonts, image fidelity and Slovene PDF text require actual sample inspection.

The source guard lists names only and rejects every untracked entry outside the
three protected root paths, including ignored files/directories. It deliberately
does not use `--exclude-standard`: ignored Python/bytecode, native extensions,
scripts, a local venv, caches or other unknown files can invalidate source review.
Protected paths are excluded by root-anchored Git pathspecs; do not inspect their
contents or use them as an executable source location. An empty NUL-separated
names file is required, and Git failure stops the procedure. No file-content scan,
cleanup, patch application, staging or reset is performed. On failure preserve the
checkout and all data; prepare a separate approved source checkout manually.
The shell disables bytecode writes during installation. Re-run source checks
before activation after any checkout use. Other Python execution may create
caches, which this strict guard also rejects; never delete
local files automatically to make the guard pass. This is an operator verification
step, not a runtime source sandbox or a claim of continuous tamper protection.

## F–G. Choose exactly one installation procedure

### NEW INSTALLATION — no existing business database

Use this only after confirming there is no existing installation/database to
preserve. The example root must not already exist, even as a dangling symlink.
Never delete or move something to make this guard pass. Existing installations
must use the next subsection; restoration must use the recovery subsection.

```sh
APP_DATA="$HOME/Library/Application Support/MarjanApp"
if [ -e "$APP_DATA" ] || [ -L "$APP_DATA" ]; then
  printf '%s\n' 'STOP: existing installation; investigate without changing it.' >&2
  exit 1
fi
mkdir -m 700 "$APP_DATA"
mkdir -m 700 "$APP_DATA/config" "$APP_DATA/data" "$APP_DATA/media" "$APP_DATA/rendered"
export MARJAN_DB_PATH="$APP_DATA/data/marjan.sqlite3"
export MARJAN_MEDIA_ROOT="$APP_DATA/media"
export MARJAN_RENDERING_ROOT="$APP_DATA/rendered"
export APP_CONFIG="$APP_DATA/config/server.json"
export APP_SECRET="$APP_DATA/config/django-secret"
unset DJANGO_SETTINGS_MODULE
"$APP_VENV/bin/python" -m app.provision_database --check-paths
```

Inspect all three displayed canonical absolute paths. Require local private disk,
outside Git; do not put the live DB in iCloud/Dropbox or a network mount. Only
after explicit approval of this NEW installation:

```sh
"$APP_VENV/bin/python" -m app.provision_database --confirm-new-install
```

Require exit zero and `New installation database provisioned and verified:`.
The existing provisioner migrates a new in-memory DB and publishes exclusively;
existing targets/sidecars are refused. No company, counter, SMTP approval or setup
completion is fabricated. On any failure retain all files and stop; do not retry
against a partial target or change an existing DB.

### EXISTING INSTALLATION — preserve the existing database

Do not run provisioning, `migrate`, restore, or the new-directory commands above.
Stop the old server cleanly and confirm no other writer, desktop wrapper, or
legacy `runserver` is running. Retain the documented existing absolute DB/media/
rendering paths and stable Django secret. Do not copy or replace business files.
Back up through the approved existing procedure before a version transition.
Review compatibility with the approved source. Missing/mismatched migration or
schema evidence is a stop condition, requiring a separate migration plan.

Set `APP_DATA`, `MARJAN_DB_PATH`, `MARJAN_MEDIA_ROOT`, `MARJAN_RENDERING_ROOT`,
`APP_CONFIG` and `APP_SECRET` to the reviewed existing installation values before
shared commands below. Export these variables in the dedicated terminal session.
`APP_CONFIG` is the reviewed JSON file and `APP_SECRET` is its existing
`DJANGO_SECRET_KEY_FILE`; neither is necessarily the NEW installation default.
Use an existing private config directory outside media/rendered/archive roots;
verify its ownership/permissions before creating a config file. Do not overwrite
an existing secret. If the existing key's provenance is unclear, investigate it
before activation rather than rotating it during startup.

### RESTORE / RECOVERY — separate controlled procedure

Use operational manual §1, persistent-path documentation and existing recovery
commands. Stop the LaunchAgent and all application writers first. Inspect backups
offline, retain the original ZIP, validate checksums/schema and manually reconcile
numbering, SMTP UNKNOWN and archive evidence under that procedure. This launcher
does not activate a restored copy, repair it, or approve recovery. Never treat
successful `--check` as recovery approval. See the B8a report for discovered
pre-existing recovery blockers. No automatic migrations of recovered data.

## H–I. Private configuration and a stable Django key

For a NEW installation only, create a stable random secret once without printing
it. This is the Django signing key, not an SMTP password. Keep it outside Git,
media, rendered documents, local archive and backup archive inputs.
Production settings read this file directly; the launcher removes inherited
`DJANGO_SECRET_KEY` and does not export the loaded key to child processes.

```sh
export APP_DATA
"$APP_VENV/bin/python" - <<'PY'
import os, secrets
from pathlib import Path
target = Path(os.environ['APP_SECRET'])
descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
    stream.write(secrets.token_urlsafe(64))
PY
```

Create a new private JSON config, filling canonical absolute paths for this
installation. For EXISTING installations use the preserved paths/key, not these
example values. This example contains only path and SMTP-reference placeholders:

```json
{
  "MARJAN_DB_PATH": "/Users/OPERATOR/Library/Application Support/MarjanApp/data/marjan.sqlite3",
  "MARJAN_MEDIA_ROOT": "/Users/OPERATOR/Library/Application Support/MarjanApp/media",
  "MARJAN_RENDERING_ROOT": "/Users/OPERATOR/Library/Application Support/MarjanApp/rendered",
  "DJANGO_SECRET_KEY_FILE": "/Users/OPERATOR/Library/Application Support/MarjanApp/config/django-secret",
  "MARJAN_SMTP_HOST": "OPERATOR_FILL_PROVIDER_HOST",
  "MARJAN_SMTP_PORT": "OPERATOR_FILL_PROVIDER_PORT",
  "MARJAN_SMTP_SECURITY": "OPERATOR_FILL_STARTTLS_OR_SSL",
  "MARJAN_SMTP_TIMEOUT_SECONDS": "OPERATOR_FILL_PROVIDER_TIMEOUT",
  "MARJAN_KEYRING_SERVICE": "OPERATOR_FILL_EXISTING_KEYCHAIN_SERVICE"
}
```

For a NEW installation, this command creates the four required fields with the
already reviewed paths and leaves SMTP readiness unconfigured. It refuses an
existing config. Add only the optional non-secret provider/reference values above
using a private editor before separately authorized SMTP acceptance.

```sh
"$APP_VENV/bin/python" - <<'PY'
import json, os
from pathlib import Path
values = {name: os.environ[name] for name in ('MARJAN_DB_PATH', 'MARJAN_MEDIA_ROOT', 'MARJAN_RENDERING_ROOT')}
values['DJANGO_SECRET_KEY_FILE'] = os.environ['APP_SECRET']
target = Path(os.environ['APP_CONFIG'])
descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
    json.dump(values, stream, indent=2)
PY
```

For an EXISTING installation retain its reviewed private config/key; if a new
configuration file is needed, review its values and use an unused file name rather
than overwriting the old config. Set files to 0600 and their parent directories to 0700; require
current-user ownership. Paths must already exist, be distinct, outside Git/TEST,
and contain no symlinks or hardlinked private files. Never put an SMTP password
in this JSON, environment, Git, a plist, or a log. The application still uses the
existing macOS `keyring`/Keychain service and company credential-key references.
The operator installs/verifies Keychain credentials through the existing approved
secure process, without echoing or passing passwords as command arguments.

Missing SMTP reference values may be omitted during first-run setup; the existing
readiness gates block issuance until explicitly configured and verified. Production
delivery remains enabled through the original boundaries; it is never replaced by
TEST delivery. Do not trigger a real SMTP test/send as part of B8a verification.

```sh
chmod 600 "$APP_CONFIG" "$APP_SECRET" "$MARJAN_DB_PATH"
"$APP_VENV/bin/python" -m app.local_server --config "$APP_CONFIG" --check
```

`--check` reads the configured DB only, requires the exact migration set and model
columns/primary/foreign keys, and runs SQLite integrity/foreign-key checks. It
does not migrate, provision, restore, change archive roots, or open a listener.
It is not a full forensic validation of a foreign/restored database. The explicit
configured path is the operator's installation identity; it is not an automatic
database-provenance/activation receipt. A wrong but structurally identical business
DB cannot be identified from schema alone. Verify its provenance separately.

## J–N. Manual production start, browser and controlled restart

```sh
cd "$APP_SOURCE"
"$APP_VENV/bin/python" -m app.local_server --config "$APP_CONFIG"
```

Require the fixed `MarjanApp listening at http://127.0.0.1:8000/` message. The
foreground process must remain running. In a second terminal verify the listener
before opening a browser:

```sh
lsof -nP -iTCP:8000 -sTCP:LISTEN
curl --noproxy '*' -I http://127.0.0.1:8000/
curl --noproxy '*' -I http://127.0.0.1:8000/static/ui/app.css
```

Require exactly the approved Python process listening on `127.0.0.1:8000`, with no
IPv6/wildcard/external listener. Do not stop an unknown process occupying port
8000; investigate. The launcher exits nonzero without selecting another port.
Use only `http://127.0.0.1:8000/`, not `localhost`, aliases, or another port.

Open that URL manually in a normal browser. A provisioned empty DB must open the
existing first-run wizard, with issuance still blocked. Enter only synthetic data
during acceptance. Verify CSS, forms, CSRF, Slovene text, sample PDF views/downloads,
logo/signature and financial output. Production HTML omits external font requests;
system fonts remain, and unavailable decorative icon ligatures are hidden while
labels remain readable. No local icon font/JavaScript bundle currently exists.
Native PDF/visual fidelity remains an explicit handoff check.

Close/reopen the browser and confirm saved setup/application state persists.
Stop the foreground server with `Ctrl+C`; require `MarjanApp stopped.` and no
remaining listener before restarting with the same command/config. The stable key
and SQLite paths must remain unchanged. Never delete `.marjan.sqlite3.server.lock`:
its kernel lock releases when the actual process exits, including after a crash.

Shutdown stops accepting requests, cancels queued work and waits for active work
before closing sockets and releasing the DB-specific lock. Buffered responses get
a bounded drain. Do not force-kill during PDF, SQLite, archive or SMTP operations.
After an abnormal termination retain DB sidecars, inspect integrity/readiness and
follow the existing UNKNOWN/recovery procedure. No automatic retry/send/repair.

## O–P. Optional operator-approved per-user LaunchAgent

Do not install/activate anything until manual native checks and operator approval
are complete. Never use a system LaunchDaemon, root, or a TEST launcher. The
source template `packaging/macos/si.marjanapp.local.plist.example` is opt-in:
`RunAtLoad=false`, `KeepAlive=false`, and only a public native-library search path
in `EnvironmentVariables`. Its absolute Python, source, config and Homebrew paths
must be filled with reviewed values. No secret value or SMTP password belongs there.

Prepare a new plist without replacing an existing agent:

```sh
APP_AGENT="$HOME/Library/LaunchAgents/si.marjanapp.local.plist"
test ! -e "$APP_AGENT" && test ! -L "$APP_AGENT" || exit 1
install -d -m 700 "$HOME/Library/LaunchAgents"
export APP_SOURCE APP_VENV APP_CONFIG APP_NATIVE_LIBS APP_AGENT
"$APP_VENV/bin/python" - <<'PY'
import os, plistlib
from pathlib import Path
source = Path(os.environ['APP_SOURCE']) / 'packaging/macos/si.marjanapp.local.plist.example'
value = plistlib.loads(source.read_bytes())
value['ProgramArguments'] = [os.environ['APP_VENV'] + '/bin/python', '-m', 'app.local_server', '--config', os.environ['APP_CONFIG']]
value['WorkingDirectory'] = os.environ['APP_SOURCE']
value['EnvironmentVariables'] = {'DYLD_FALLBACK_LIBRARY_PATH': os.environ['APP_NATIVE_LIBS']}
with Path(os.environ['APP_AGENT']).open('xb') as stream:
    plistlib.dump(value, stream)
PY
plutil -lint "$APP_AGENT"
```

Inspect the prepared plist and approved paths. No agent has been loaded yet.
After explicit approval, with the manual server already stopped:

```sh
launchctl bootstrap "gui/$(id -u)" "$APP_AGENT"
launchctl kickstart "gui/$(id -u)/si.marjanapp.local"
launchctl print "gui/$(id -u)/si.marjanapp.local"
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

To stop/restart, drain active work and verify the old listener is gone. Do not use
`kickstart -k`, SIGKILL, or remove a lock to force a restart:

```sh
launchctl bootout "gui/$(id -u)" "$APP_AGENT"
lsof -nP -iTCP:8000 -sTCP:LISTEN || true
```

Require no app listener/process before another approved `bootstrap` + `kickstart`.
`ExitTimeOut=0` requests an unlimited graceful termination period; a stuck active
operation can delay logout/stop and needs operator investigation. Verify the
actual target macOS behavior; this has not been tested on Windows.

Automatic startup at login is a separate approval: after successful manual agent
acceptance, the operator may change only `RunAtLoad` to true, lint/review the plist,
then bootout/bootstrap it. Confirm behavior after logout/login and reboot with
the normal user session and unlocked login Keychain. `KeepAlive=false` deliberately
avoids an automatic restart loop; after a crash investigate before manual restart.

## Q–R. Backup, failure, rollback and acceptance gates

Use operational manual §1 for snapshots and offline recovery. Keep all writers and
agents stopped during restore/reconciliation. Preserve the old reviewed source,
venv, stable key and configuration until a verified backup and rollback decision
exist. A code rollback is allowed only when the old revision matches the existing
schema; do not run reverse migrations or replace a business DB automatically.
Never reset onboarding or invoice sequences to make a server start.

On absent DB, permissions/path mismatch, corrupt/wrong schema, occupied port,
duplicate instance, missing CSS/native libraries or failed shutdown: stop, retain
data and investigate. The CLI emits fixed credential-free feedback; no debug
traceback or private request/credential logging is enabled. `--check` distinguishes
configuration/DB failure from subsequent bind/lock/runtime failure.

Required evidence before the October 29 production handoff: approved commit,
resolved dependency/native versions, canonical paths/permissions, new-versus-
existing installation decision, listening interface, browser/CSRF/static/PDF,
normal and in-flight shutdown, crash/restart/reboot, Keychain and separately
authorized SMTP acceptance, backup/recovery and rollback readiness. Windows tests
do not establish these native results. Recovery blockers in the B8a report and
uncompleted business readiness remain blockers, even if the server starts.

References: [Waitress arguments](https://docs.pylonsproject.org/projects/waitress/en/latest/arguments.html),
[WhiteNoise WSGI](https://whitenoise.readthedocs.io/en/stable/base.html),
[WeasyPrint native requirements](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html),
[Apple LaunchAgents](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html),
[Apple ExitTimeOut](https://github.com/apple-oss-distributions/launchd/blob/main/man/launchd.plist.5).
