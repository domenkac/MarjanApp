"""Explicit local production configuration; no provisioning or TEST defaults."""

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
REQUIRED = {
    "MARJAN_DB_PATH", "MARJAN_MEDIA_ROOT", "MARJAN_RENDERING_ROOT", "DJANGO_SECRET_KEY_FILE",
}
SMTP_SETTINGS = {
    "MARJAN_SMTP_HOST", "MARJAN_SMTP_PORT", "MARJAN_SMTP_SECURITY",
    "MARJAN_SMTP_TIMEOUT_SECONDS", "MARJAN_KEYRING_SERVICE",
}


class ServerError(Exception):
    """Private diagnostics must not be interpolated into public errors."""


def private_path(value, *, directory=False):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ServerError("An explicit absolute path is required.")
    path = Path(value)
    if (not path.is_absolute() or path.drive.startswith("\\\\") or path != path.resolve(strict=True)
            or path.is_relative_to(REPOSITORY) or "MarjanApp-TEST" in path.parts):
        raise ServerError("Production storage must be canonical and outside source/TEST.")
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or (not directory and info.st_nlink != 1):
        raise ServerError("Production files must not be aliases or hard links.")
    if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ServerError("Production paths require private ownership and permissions.")
    return path


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ServerError("Duplicate configuration key.")
        result[name] = value
    return result


@dataclass(frozen=True)
class Configuration:
    path: Path
    database: Path
    media: Path
    rendered: Path
    secret: str = field(repr=False)
    smtp: dict = field(repr=False)


def load_configuration(value):
    path = private_path(value)
    private_path(str(path.parent), directory=True)
    if path.stat().st_size > 16_384:
        raise ServerError("Configuration is too large.")
    values = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if (not isinstance(values, dict) or not REQUIRED <= values.keys()
            or values.keys() - REQUIRED - SMTP_SETTINGS):
        raise ServerError("Production configuration fields are missing or unsupported.")
    database = private_path(values["MARJAN_DB_PATH"])
    private_path(str(database.parent), directory=True)
    media = private_path(values["MARJAN_MEDIA_ROOT"], directory=True)
    rendered = private_path(values["MARJAN_RENDERING_ROOT"], directory=True)
    secret_path = private_path(values["DJANGO_SECRET_KEY_FILE"])
    private_path(str(secret_path.parent), directory=True)
    for left, right in ((media, rendered), (media, database), (rendered, database),
                        (media, secret_path), (rendered, secret_path),
                        (media, path), (rendered, path)):
        if left.is_relative_to(right) or right.is_relative_to(left):
            raise ServerError("Private configuration and mutable data must not overlap.")
    if secret_path.stat().st_size > 1024:
        raise ServerError("Invalid installation secret.")
    secret = secret_path.read_text(encoding="ascii").rstrip("\n")
    if len(secret) < 50 or any(character.isspace() for character in secret):
        raise ServerError("A stable private installation secret is required.")
    smtp = {name: values.get(name, "") for name in SMTP_SETTINGS}
    if any(not isinstance(value, str) for value in smtp.values()):
        raise ServerError("SMTP references must be strings, never credentials.")
    return Configuration(path, database, media, rendered, secret, smtp)


def configure_environment(configuration):
    for name in tuple(os.environ):
        if name.startswith(("MARJAN_", "DJANGO_")):
            del os.environ[name]
    os.environ.update({
        "MARJAN_SERVER_CONFIG": str(configuration.path),
        "DJANGO_SETTINGS_MODULE": "app.production_settings",
        "DJANGO_DEBUG": "0",
        "MARJAN_DB_PATH": str(configuration.database),
        "MARJAN_MEDIA_ROOT": str(configuration.media),
        "MARJAN_RENDERING_ROOT": str(configuration.rendered),
        **configuration.smtp,
    })
