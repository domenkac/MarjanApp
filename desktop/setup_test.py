"""Operator-only first installation from source; never called by the .app."""

import argparse
import json
import os
import subprocess
import sys

from desktop.profile import ProfileLock, default_profile, new_secret, write_private


def provision(profile):
    if getattr(sys, "frozen", False):
        raise ValueError("Use a source virtual environment for explicit TEST setup.")
    if not profile.root.is_absolute() or profile.root.resolve() != profile.root:
        raise ValueError("TEST setup path must not contain aliases or symlinks.")
    # Refuse any previous installation, including a partial/failed one.
    profile.root.mkdir(mode=0o700, parents=True, exist_ok=False)
    for path in profile.directories:
        if path != profile.root:
            path.mkdir(mode=0o700)
        profile.private(path, directory=True)
    with ProfileLock(profile):
        write_private(profile.root / "secret.key", new_secret())
        environment = {k: v for k, v in os.environ.items() if not k.startswith(("MARJAN_", "DJANGO_"))}
        environment.update({
            "DJANGO_SETTINGS_MODULE": "app.settings",
            "DJANGO_DEBUG": "0",
            "MARJAN_DB_PATH": str(profile.database),
            "MARJAN_MEDIA_ROOT": str(profile.root / "media"),
            "MARJAN_RENDERING_ROOT": str(profile.root / "rendered"),
        })
        result = subprocess.run(
            [sys.executable, "-m", "app.provision_database", "--confirm-new-install"],
            env=environment, capture_output=True, timeout=120, check=False,
        )
        if result.returncode:
            raise ValueError("Safe TEST provisioning failed. Retain the directory for operator diagnosis; do not reuse it.")
        info = profile.private(profile.database)
        write_private(profile.root / "profile.json", json.dumps({
            "profile": "TEST", "version": 1, "database_id": [info.st_dev, info.st_ino],
        }))
        profile.load()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-new-test", action="store_true", required=True)
    parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Run explicit TEST setup on the target Mac only.")
    try:
        provision(default_profile())
    except Exception:  # noqa: BLE001 -- operator boundary, no configuration disclosure
        print("TEST setup failed. No existing installation may be overwritten or reused. See docs/desktop-macos.md.", file=sys.stderr)
        return 1
    print("New private TEST installation ready. Open MarjanApp.app. Initial business setup remains incomplete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
