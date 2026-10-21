# T12-B7: first native Mac TEST acceptance — Friday 2026-10-23

Operator checklist for the existing T11-A launcher. Use only an isolated Apple
Silicon TEST account and synthetic records. Follow `desktop-macos.md` for the
installation model. Independent review of cumulative T12-B7 + T12-B7a must finish
before this procedure.
No production activation, database import, SMTP delivery, Keychain authentication,
or real cloud folders are part of acceptance.

Record each check as PASS, FAIL, or NOT TESTED with evidence. A written procedure
does not establish acceptance. Windows results do not establish native behavior.

## A–D. Native prerequisites and source environment

### Preferred route: independently approved GitHub commit

Use the exact independently approved GitHub commit in `~/MarjanApp`. Obtain its
full commit SHA from the independent approval record. The final approved SHA is
not known when this checklist is written: the operator must replace the labeled
placeholder below. An unchanged placeholder, a different HEAD, or tracked changes
must terminate the procedure before creating the virtual environment.

Run the blocks in the same dedicated terminal session. If the session terminates,
repeat source verification before continuing. Do not automatically pull, checkout,
reset, clean, or apply a patch to make a source tree match. Investigate mismatches
and untracked build inputs separately. A committed build requires no review patch.

```sh
set -eu
cd "$HOME/MarjanApp"
umask 077
EXPECTED_APPROVED_COMMIT='OPERATOR_FILL_40_CHARACTER_APPROVED_GITHUB_COMMIT_SHA'
if ! printf '%s\n' "$EXPECTED_APPROVED_COMMIT" | LC_ALL=C grep -Eq '^[0-9a-f]{40}$'; then
  printf '%s\n' 'STOP: supply the full independently approved GitHub commit SHA.' >&2
  exit 1
fi
ACTUAL_SOURCE_COMMIT=$(git rev-parse --verify HEAD)
if [ "$ACTUAL_SOURCE_COMMIT" != "$EXPECTED_APPROVED_COMMIT" ]; then
  printf '%s\n' 'STOP: HEAD differs from the independently approved commit.' >&2
  exit 1
fi
if ! git diff --quiet --ignore-submodules=none || ! git diff --cached --quiet --ignore-submodules=none; then
  printf '%s\n' 'STOP: the tracked worktree or index is not clean.' >&2
  exit 1
fi
git rev-parse HEAD
git status --short --branch --untracked-files=normal
export EXPECTED_APPROVED_COMMIT
```

### Separate route: explicitly authorized uncommitted review state

Use this route only when independent review explicitly authorizes an uncommitted
build. It is not a fallback for a failed committed-source check. Before any venv
creation, obtain a separate verification record from the reviewer containing:

- The exact base commit SHA and explicit authorization for this uncommitted state.
- The approved complete cumulative patch and its SHA-256, including every new file.
- A complete source manifest with hashes for all build inputs, including new files.

Verify HEAD against that base SHA, verify the patch digest and source manifest
against the independent record, and compare the current cumulative changes,
including new files and the index, with the approved patch. Investigate every
additional untracked build input. Record the authorization, comparison results,
HEAD, Git status, patch hash, and manifest in a separate evidence directory.
Stop if evidence is missing or any content differs. Do not apply the patch,
silently alter files, or merely skip the preferred route's clean-tree guard.
Only after this separate verification may the operator continue with the native
prerequisites and environment steps below. The review patch is required only for
this explicitly authorized route, not for an approved committed build.

### Native prerequisites and environment

The following assumes one source route above has passed. For the separate route,
start a dedicated shell with `set -eu`, use `~/MarjanApp`, and retain its separate
source-verification evidence before continuing.

```sh
uname -m
test "$(uname -m)" = arm64
sysctl -in sysctl.proc_translated 2>/dev/null || true
xcode-select -p
brew --version
brew --prefix
python3 --version
python3 -c 'import platform,sys; assert platform.machine() == "arm64"; assert sys.version_info >= (3,12); print(sys.executable)'
```

Require native arm64 Python 3.12+, Xcode command-line tools, and native Homebrew.
Do not use `/usr/bin/python3` or a translated Python. `sysctl.proc_translated` must
not report `1`. Install missing prerequisites through the normal Mac operator
process before continuing. No global security settings may be disabled.

```sh
TEST_EVIDENCE=$(mktemp -d "$HOME/MarjanApp-TEST-evidence-20261023.XXXXXX")
export TEST_EVIDENCE
git rev-parse HEAD > "$TEST_EVIDENCE/source-head.txt"
git status --short --branch --untracked-files=normal > "$TEST_EVIDENCE/source-status.txt"
sw_vers > "$TEST_EVIDENCE/macos.txt"
python3 --version > "$TEST_EVIDENCE/python.txt"
test ! -e .venv-desktop
python3 -m venv .venv-desktop
source .venv-desktop/bin/activate
python -m pip install -r requirements-desktop.txt > "$TEST_EVIDENCE/pip-install.txt" 2>&1
python -m pip check
python -m pip freeze > "$TEST_EVIDENCE/python-versions.txt"
python -m pip --version > "$TEST_EVIDENCE/pip.txt"
```

The existing desktop pins are Waitress 3.0.2, WhiteNoise 6.12.0, pywebview 6.1,
and PyInstaller 6.22.0. `requirements.txt` still contains ranges and unpinned
packages. This recipe is repeatable, but the supply chain is not fully locked.
Retain the resolved native dependency list; do not claim bit-identical builds.
Failure to resolve a pinned release is a build blocker, not permission to upgrade.

## E–G. Native staging, build, and provenance

Use a new staging directory and fresh build output. Stop if any target exists;
preserve it for investigation. Never reuse or delete an unknown installation.

```sh
brew install pango harfbuzz fontconfig glib
brew list --versions > "$TEST_EVIDENCE/native-versions.txt"
test ! -e build-native-arm64
test ! -e build/MarjanApp
test ! -e dist/MarjanApp
test ! -e dist/MarjanApp.app
python packaging/macos/stage_native.py "$PWD/build-native-arm64" > "$TEST_EVIDENCE/staging.txt" 2>&1
find build-native-arm64 -type f -name '*.dylib' -exec lipo -archs {} \; > "$TEST_EVIDENCE/staged-architectures.txt"
find build-native-arm64 -type f -name '*.dylib' -exec otool -L {} \; > "$TEST_EVIDENCE/staged-dependencies.txt"
export MARJAN_BUILD_NATIVE_LIBS="$PWD/build-native-arm64"
python -m PyInstaller --clean --noconfirm packaging/macos/MarjanApp.spec > "$TEST_EVIDENCE/pyinstaller.txt" 2>&1
TEST_APP="$PWD/dist/MarjanApp.app"
export TEST_APP
file "$TEST_APP/Contents/MacOS/MarjanApp"
lipo -archs "$TEST_APP/Contents/MacOS/MarjanApp"
find "$TEST_APP" -type f -name '*.dylib' -exec otool -L {} \; > "$TEST_EVIDENCE/bundled-dependencies.txt"
find "$TEST_APP" -type f -name '*.dylib' -exec lipo -archs {} \; > "$TEST_EVIDENCE/bundled-architectures.txt"
find "$TEST_APP" -type f > "$TEST_EVIDENCE/bundle-files.txt"
find "$TEST_APP" \( -name '*.sqlite3' -o -name '*.sqlite3-*' -o -name 'profile.json' -o -name 'secret.key' -o -name 'nespremenljivi-asseti' -o -name 'slike' \) -print
```

Require arm64 slices, complete native closure, and no unresolved build-machine
Homebrew dependency. Inspect PyInstaller warnings, both invoice templates, UI
templates/CSS, migrations, XSDs, WeasyPrint data, Cocoa modules, and keyring backend
in the bundle. The final `find` must report no user data. Inspect all other bundle
files for unintended configuration or media. Preserve dependency licenses.

Test on an isolated account/Mac where the app cannot use the build venv, Python,
or Homebrew libraries. Do not remove or rename shared installations to simulate
this. A build that succeeds only on the builder is not accepted.

## H. Signing and Gatekeeper

```sh
codesign --display --verbose=4 "$TEST_APP" 2> "$TEST_EVIDENCE/signature.txt"
codesign --verify --deep --strict --verbose=2 "$TEST_APP" > "$TEST_EVIDENCE/signature-verification.txt" 2>&1
spctl --assess --type execute --verbose=4 "$TEST_APP" > "$TEST_EVIDENCE/gatekeeper.txt" 2>&1
xattr -l "$TEST_APP" > "$TEST_EVIDENCE/extended-attributes.txt"
```

Record exit codes, signer, notarization status, quarantine, and actual transfer
method. An ad-hoc signature does not establish Developer ID signing or
notarization. Gatekeeper rejection is an acceptance blocker requiring operator
review. Do not clear quarantine or globally disable Gatekeeper as a workaround.

## I–J. Explicit fresh TEST installation and Finder launch

```sh
TEST_PROFILE="$HOME/Library/Application Support/MarjanApp-TEST"
export TEST_PROFILE
if [ -e "$TEST_PROFILE" ] || [ -L "$TEST_PROFILE" ]; then
  printf '%s\n' 'STOP: existing or partial TEST profile; operator investigation required.' >&2
  exit 1
fi
printf '%s\n' 'Fresh TEST profile location confirmed.'
```

If the profile exists or is a symlink, **stop**. Do not provision, overwrite, delete, move, reset, reuse,
replace its database, or edit its receipt. Preserve the profile for investigation.

Before provisioning, reveal the app, double-click it in Finder, and dismiss the
expected missing-installation error. Require no profile/database creation and no
remaining process or listening server.

```sh
open -R "$TEST_APP"
test ! -e "$TEST_PROFILE"
pgrep -x MarjanApp || true
```

After that negative check, explicitly provision the new TEST installation:

```sh
source .venv-desktop/bin/activate
if [ -e "$TEST_PROFILE" ] || [ -L "$TEST_PROFILE" ]; then
  printf '%s\n' 'STOP: TEST profile appeared before provisioning; investigate without changing it.' >&2
  exit 1
fi
python -m desktop.setup_test --confirm-new-test
stat -f '%Sp %Su %N' "$TEST_PROFILE" "$TEST_PROFILE/test.sqlite3" "$TEST_PROFILE/profile.json" "$TEST_PROFILE/secret.key"
```

Require owner-only directories 0700 and files 0600. Do not display `secret.key`.
Setup runs provisioning migrations once; app startup never does. Business tables,
approvals, and setup completion must remain empty until the operator enters data.

Record the initial schema/migration set with the source venv, read-only:

```sh
python - <<'PY'
import json, os, sqlite3
from contextlib import closing
from pathlib import Path
database = Path(os.environ['TEST_PROFILE']) / 'test.sqlite3'
with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
    snapshot = {
        'schema': db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall(),
        'migrations': db.execute('SELECT app,name FROM django_migrations ORDER BY app,name').fetchall(),
    }
(Path(os.environ['TEST_EVIDENCE']) / 'schema-before.json').write_text(json.dumps(snapshot), encoding='utf-8')
PY
open -R "$TEST_APP"
```

Double-click in Finder. Require one native window titled **MarjanApp TEST**, no
Terminal/server console or external browser, and a clear TEST label. Missing
libraries/resources must produce a controlled error, not a fallback deployment.

## K–M. Offline UI and synthetic PDF

- Disconnect networking. Navigate forms, CSS, setup, and PDF preview. Check Slovene
  text, local font fallback, and missing Material Symbols. Record visual blockers.
  Confirm no Google Fonts or other external requests using operator network tools.
- Enter only synthetic companies, tenant, property, lease, VAT, and PNG/JPEG logo
  and signature. Do not import production configuration or approve setup through
  direct database writes. SMTP readiness and complete production setup remain
  unavailable in TEST.
- Enter only these exact synthetic archive paths in Settings → Archive:

  ```text
  ~/Library/Application Support/MarjanApp-TEST/archive-local
  ~/Library/Application Support/MarjanApp-TEST/archive-icloud-test
  ~/Library/Application Support/MarjanApp-TEST/backup-dropbox-test
  ```

  Expand `~` to the account's absolute home path in the form. These are local
  substitutes. No iCloud/Dropbox connection is involved.
- Generate a **sample PDF** through the existing setup UI. Inspect actual PDF for
  issuer identity, both original images, `č Č š Š ž Ž`, dates, financial totals,
  VAT, layout, fonts, and the sample watermark. Confirm no invoice number is
  allocated. Test WebKit PDF display and return navigation inside the native app.
- Retain sample PDF SHA-256 and screenshots containing only synthetic data. Do
  not claim issued NP/ST acceptance on Mac from a sample-only check. Issuance is
  constrained by the unchanged TEST SMTP readiness policy; record that boundary.

## N–Q. Persistence, locks, isolation, and shutdown

Quit normally; reopen in Finder and confirm synthetic state persists. While the
app is open, inspect its process and listener without printing capability URLs:

```sh
pgrep -x MarjanApp
TEST_PID=$(pgrep -x MarjanApp)
lsof -nP -a -p "$TEST_PID" -iTCP -sTCP:LISTEN
open -n "$TEST_APP"
```

Require exactly one original app process/listener on `127.0.0.1`, a useful rejection
of the second process, and no second installation. Finder may otherwise focus the
existing app, so `open -n` is necessary for this check. Never delete `desktop.lock`.

Quit, then test inherited forbidden settings using harmless nonexistent sentinels:

```sh
env MARJAN_DB_PATH=/nonexistent/MarjanApp-production-forbidden.sqlite3 MARJAN_MEDIA_ROOT=/nonexistent/MarjanApp-production-media MARJAN_RENDERING_ROOT=/nonexistent/MarjanApp-production-rendered MARJAN_SMTP_HOST=production-forbidden.invalid MARJAN_KEYRING_SERVICE=production-forbidden DJANGO_SETTINGS_MODULE=production.forbidden DJANGO_DEBUG=1 "$TEST_APP/Contents/MacOS/MarjanApp"
```

Require the same fixed TEST state and DEBUG=False behavior. No sentinel path may
be created or opened. Do not substitute real production paths or credentials.
Attempt TEST SMTP actions only with synthetic values; require explicit rejection,
no SMTP connection and no Keychain prompt/read. Confirm private archive substitutes
are the only usable roots and no background cloud/backup/delivery process starts.
Never select a real cloud location for a negative test.

Check missing/wrong capability, wrong Host/Origin, cross-site request, and missing
CSRF rejection in a controlled local browser session. Do not save the capability
URL in evidence or logs. Record response statuses only. Windows gateway tests
cover these boundaries synthetically; native verification still requires evidence.

Quit normally and during a sample PDF request. Wait for active work to finish.
Require window, process, worker threads, and listener to close before restart.

```sh
pgrep -x MarjanApp || true
lsof -nP -a -p "$TEST_PID" -iTCP -sTCP:LISTEN || true
```

Both must show no surviving app/listener. Reopen after Quit and reboot. Test Force
Quit separately; require released OS lock and normal subsequent launch. Do not
mark graceful in-flight shutdown passed based on Force Quit.

## R. No implicit migrations or new database

After quitting, compare schema and applied migrations, not the whole database
hash: intentional UI data entry and saved setup navigation can change database
bytes. Use the source venv only for this read-only check:

```sh
python - <<'PY'
import json, os, sqlite3
from contextlib import closing
from pathlib import Path
database = Path(os.environ['TEST_PROFILE']) / 'test.sqlite3'
with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
    snapshot = {
        'schema': db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall(),
        'migrations': db.execute('SELECT app,name FROM django_migrations ORDER BY app,name').fetchall(),
    }
evidence = Path(os.environ['TEST_EVIDENCE'])
(evidence / 'schema-after.json').write_text(json.dumps(snapshot), encoding='utf-8')
assert json.loads((evidence / 'schema-before.json').read_text()) == json.loads(json.dumps(snapshot))
print('Schema and migration set unchanged.')
PY
find "$TEST_PROFILE" -maxdepth 1 -type f -name '*.sqlite3*' -print
```

Require only the provisioned database and legitimate SQLite sidecars. The initial
missing-profile launch establishes no implicit first-run database creation. Do
not delete or rename this accepted profile to repeat that check. Test incompatible
schema/replaced receipt only in a separately authorized disposable Mac installation.

## Acceptance ledger

- WINDOWS_STATIC_CHECK — performed; see T12-B7a report for limits/blockers.
- WINDOWS_SYNTHETIC_TESTS — see actual test results in T12-B7a report.
- MAC_NATIVE_BUILD — NOT TESTED.
- MAC_INSTALLATION — NOT TESTED.
- MAC_RUNTIME — NOT TESTED.
- MAC_PDF_RENDER — NOT TESTED.
- MAC_SECURITY — NOT TESTED.
- PRODUCTION_READY — NO.

Retain build, signing, quarantine, dependency-locking, offline fonts/icons, PDF
navigation, shutdown, and clean-machine portability blockers until actual checks
pass. Retain the historical navigation-test issue in the final review ledger;
the current narrow navigation assertion result is recorded separately in B7.
Archive navigation is still disabled in the current sidebar and is outside B7.
