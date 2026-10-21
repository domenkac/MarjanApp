# MarjanApp TEST desktop preparation — T11-A

Status: source preparation only. No macOS bundle has been built or accepted on
Windows. T11-B must build and test on an actual Apple Silicon Mac. This is an
isolated TEST deployment, not production activation or recovery.

This explicitly requested desktop proof of concept adds a native wrapper to the
venv deployment in SAD v1.2 §1, “Odločitev o skladu”. The existing venv/browser
workflow in the operational manual §6 remains available. Record the approved
desktop TEST deployment alongside SAD §1 and operational manual §6 when this
change is accepted. No financial or delivery state rules are changed.

## Runtime and security

`desktop_entry.py` starts a pywebview Cocoa/WebKit window on the main thread.
Waitress runs Django in two worker threads in the same process, without runserver,
autoreload, subprocess servers, a Terminal console, or an external browser.
Waitress atomically binds `127.0.0.1:0`. An authenticated readiness request must
succeed before the window is created. Returning from the GUI event loop (including
Quit) stops accepting work, cancels queued work, waits for active operations,
closes all server channels, and joins the server thread before releasing the lock.
A synchronous closing handler also performs shutdown before Cocoa native Quit can
terminate the process without returning from its event loop.
Quitting can wait for an in-flight PDF or database operation. Force Quit is not a
graceful shutdown and requires separate Mac acceptance checks.

Each launch mounts every route and static resource under a fresh 256-bit random
capability path. Unauthenticated requests, other Host/port combinations, other
Origins, and `Sec-Fetch-Site: same-site/cross-site` requests are denied before
Django or static serving. A random port is not the access control. The capability
is not persisted or logged. Responses have `no-store`, `no-referrer`, an offline
CSP, and no CORS access grant. Django CSRF and template escaping remain enabled.
The capability is not a defense against malware running as the same OS user.

The fixed profile is:

```text
~/Library/Application Support/MarjanApp-TEST/
    profile.json                 TEST version and provisioned database identity
    secret.key                   stable random installation secret
    desktop.lock                 kernel lock; do not delete while running
    test.sqlite3                 existing database only; SQLite mode=rw
    media/                       uploads
    rendered/                    generated documents
    archive-local/               synthetic local archive
    archive-icloud-test/         synthetic iCloud substitute; no cloud connection
    backup-dropbox-test/         synthetic Dropbox substitute; no cloud connection
```

There is no production profile selector or path override. The launcher discards
inherited `MARJAN_*` and `DJANGO_*` configuration. Desktop settings independently
force DEBUG=False, loopback hosts, fixed data paths and an empty SMTP configuration.
Delivery transport and credential boundaries explicitly reject TEST operations,
including injected transport/credential adapters. Django email uses a dummy
backend. Keyring is packaged for dependency completeness but TEST delivery never
reads Keychain. No background backup, delivery, launchd job, or recovery is started.

POSIX directories must be owned by the current user with no group/other access
(0700); private files require no group/other access (0600). Symlink aliases and
hardlinked private files are rejected. The database identity recorded after safe
provisioning must still match; replacing it with a recovered/copied database fails
closed. This receipt is local installation metadata, not an anti-tampering signature.
Do not edit it to activate a replacement database.

Startup never provisions or migrates. A read-only schema check requires the
installed migration set to match the packaged code. Missing, incompatible, or
replaced databases stop startup. Archive configuration already in a database must
match the three synthetic roots. Archive validation and `ArchiveRoots` also enforce
these roots before any filesystem probe. Do not import production records or files.
Keep the private directories free of symlinks to real user/cloud locations.

Persistent files are outside both the application bundle and source tree.
Resources use package `__file__` paths, which PyInstaller maps into its bundle.
WhiteNoise serves packaged static files with DEBUG=False and without collectstatic
at startup. Templates, static files, and XML schemas are explicitly collected.
The current UI has CSS but no local JavaScript asset. External Google font links
are omitted in TEST; system fonts are used. Material Symbols will not have their
remote icon font; inspect this visual limitation in T11-B. Browser deployment
retains its existing fonts. No new application icon is supplied.

## Explicit first installation on the TEST Mac

Frozen first-run provisioning is intentionally not implemented. A missing profile
shows a native error with the setup document reference and exits without creating
a database. This follows the permitted fail-closed fallback in T11-A. A source
venv is required once by the installer; normal .app use will not require Python.

Use native arm64 Python 3.12+ on the TEST Mac. Do not use Apple's system Python,
Rosetta, or a production environment. Before creating the venv, follow the source
verification gate in the [dated operator checklist](desktop-macos-acceptance-2026-10-23.md).
The preferred route requires the exact independently approved GitHub commit SHA
and a clean tracked worktree/index. The operator must fill and verify the approved
SHA; a missing value, different HEAD, or tracked change stops the procedure.
No local review patch is required after commit. Do not automatically pull,
checkout, reset, clean, or apply patches. An uncommitted build requires the separate
explicitly authorized verification route documented in that checklist.
The following commands assume source verification passed and the fixed TEST
profile does not exist, including as a symlink:

```sh
python3 -m venv .venv-desktop
source .venv-desktop/bin/activate
python -m pip install -r requirements-desktop.txt
python -m desktop.setup_test --confirm-new-test
```

The last command explicitly authorizes a **brand-new empty TEST installation**
at the fixed path above. Before running it, verify that this is the intended TEST
Mac account and that `MarjanApp-TEST` does not already exist. There is no overwrite,
reset, reuse, or production option. Setup exclusively creates private directories
and a secret, obtains the same kernel lock as the launcher, and calls the existing
`app.provision_database --confirm-new-install` in a fresh Python process with
isolated paths and the unmodified app.settings module. Existing provisioning
safeguards are unchanged: migrations run in memory, publication is exclusive, and
an existing target or sidecar is never replaced. Only successful provisioning
publishes the TEST receipt. No companies, invoices, or numbering are fabricated.

On failure, retain the partial directory for operator diagnosis. Do not rerun
against it, delete a database to reset it, or edit its receipt. A new empty profile
requires a separate operator investigation outside this TEST acceptance procedure.
This procedure never deletes, resets, moves, or overwrites an existing profile.
No automatic cleanup occurs.

After setup, double-click the reviewed `MarjanApp.app`. Complete only synthetic
initial setup records. Enter the exact three synthetic archive paths above in
Settings → Archive; setup does not insert archive business configuration. SMTP
readiness cannot be completed in this TEST profile. Test delivery must fail closed;
do not treat it as a production delivery acceptance environment.

## Build on native Apple Silicon

Build preparation files are a starting recipe, not a verified distributable.
Install Xcode command-line tools and Homebrew on the build Mac. These are build
prerequisites, not requirements for ordinary launch of the resulting app.

```sh
source .venv-desktop/bin/activate
python -c 'import platform; assert platform.machine() == "arm64"'
brew install pango harfbuzz fontconfig glib
python packaging/macos/stage_native.py "$PWD/build-native-arm64"
export MARJAN_BUILD_NATIVE_LIBS="$PWD/build-native-arm64"
python -m PyInstaller --clean --noconfirm packaging/macos/MarjanApp.spec
python -m pip freeze > build-python-versions.txt
brew list --versions > build-native-versions.txt
```

Use a new staging directory; the staging tool refuses reuse. It verifies arm64
slices, traverses the Homebrew dylib dependency closure using `otool -L`, refuses
unresolved `@rpath`/`@loader_path` dependencies or conflicting basenames, and copies
only that closure. PyInstaller then rewrites collected native dependencies. The
runtime hook directs WeasyPrint's CFFI library loads to absolute bundle paths.
WeasyPrint package data, Pango/GLib/HarfBuzz/Fontconfig and their non-system native
dependencies, pywebview Cocoa modules, keyring backends, Django application modules,
migrations, management commands, templates, CSS and XSDs are included.

The spec enforces macOS/arm64, `console=False`, and an onedir BUNDLE. Expected
artifact: `dist/MarjanApp.app`; retain the accompanying build records. Do not use a
onefile build or attempt a Windows cross-build. Never package a profile, database,
upload, secret, production configuration, or the protected reference directories.

New desktop/build dependencies are pinned in requirements-desktop.txt. Existing
requirements.txt remains unchanged and has ranges/unpinned dependencies. A fully
locked, bit-for-bit reproducible supply chain is not provided here: retain pip
freeze, Python/macOS versions, Homebrew versions, and source revision for T11-B.
Homebrew library layout changes can fail staging and require inspection on Mac.
Do not hide missing native libraries or accept a build that only works because
the builder happens to have Homebrew installed.

Signing and notarization remain T11-B checks. PyInstaller normally ad-hoc signs;
this is not Developer ID signing or notarization. Inspect Gatekeeper behavior for
the actual transfer method. Do not globally disable Gatekeeper. Inspect bundled
licenses and retain required notices before distribution.

## T11-B acceptance — actual Apple Silicon Mac required

The dated [T12-B7 operator checklist](desktop-macos-acceptance-2026-10-23.md)
provides commands and a separate acceptance ledger for Friday 2026-10-23.
Its existence does not establish native acceptance.

- Build arm64 on the TEST Mac; record source revision, Python/pip/Homebrew/macOS
  versions, staging/build output, library architecture, and signing status.
- Inspect `otool -L` on bundled dylibs. No unresolved build-machine Homebrew paths
  may remain. Validate launch and PDF output on an account/Mac without the build
  venv, Homebrew libraries, or Python installation available to the app.
- Before setup, double-click: useful native error, no empty fallback database,
  Terminal, browser, or server left running. Also test missing library/resources.
- Explicitly prepare the fresh private TEST profile, then double-click in Finder.
  Verify one dedicated native window and no visible server console.
- Verify CSS, templates, all navigation/forms, offline font fallback, and Slovene
  text. No network font requests. Inspect target=_blank PDF links: they must stay
  inside the native window. Test WebKit PDF display and returning to the UI.
- Add synthetic company/tenant/contract records and synthetic media. Verify sample
  PDF generation using bundled native libraries and usable fonts. Do not fabricate
  production readiness or send a real invoice.
- Close and reopen; TEST state and secret persist. No startup database changes,
  implicit migrations or provisioning. Start another executable instance (Finder
  may focus the current app); the second process must be rejected.
- Quit normally and during a request. Verify that the process, worker threads,
  and listening socket stop after in-flight work finishes. Reopen after quitting
  and after reboot. Check Force Quit separately; OS locks must be released.
- Test forged Host, missing/wrong capability, foreign Origin, and cross-site
  requests; all must fail. Valid POST without CSRF must fail. Check no capability
  URL, exception, secret, or local configuration appears in logs/error dialogs.
- Try inherited production environment paths and invalid/replaced TEST DB receipts;
  startup must not open production data or activate a recovered database. Check
  POSIX permissions, symlink rejection, and duplicate-launch lock on real macOS.
- Use only the synthetic archive roots. Other paths must fail before probing.
  Verify no real iCloud, Dropbox, Keychain, or SMTP activity. SMTP actions must fail.
- Record PDF, WebKit navigation, app shutdown, packaging, signing, quarantine and
  Gatekeeper blockers. Do not mark packaging accepted until these checks pass.

## Reference documentation used for preparation

- [pywebview API](https://pywebview.flowrl.com/api/) — Cocoa UI lifecycle, private
  mode, and external-link settings.
- [pywebview freezing](https://pywebview.flowrl.com/guide/freezing.html) — packaging.
- [Waitress 3.0.2 source](https://github.com/Pylons/waitress/tree/v3.0.2/src/waitress)
  — socket binding, private event map and dispatcher shutdown.
- [WhiteNoise WSGI](https://whitenoise.readthedocs.io/en/stable/base.html) — bundled
  static serving independent of Django DEBUG.
- [PyInstaller macOS](https://pyinstaller.org/en/stable/feature-notes.html#macos)
  — architecture and native bundle constraints.
- [WeasyPrint installation](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html)
  — native dependencies. Their inclusion still needs real Mac verification.
