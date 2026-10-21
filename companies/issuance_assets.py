"""Verified, sealed asset evidence for the short official-number transaction.

The seal authenticates verification, not continued filesystem availability. External
file changes after preflight remain detectable by the existing PDF integrity gate.
"""

import re
from dataclasses import dataclass
from hashlib import sha256

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import connection

from .models import Company, InitialSetupCompletion
from .services import (
    _kanonicni_fingerprint,
    izberi_veljavnega_podpisnika,
    shrani_nespremenljiv_asset,
)

_SALT = "companies.issuance-assets.v1"
_DIGEST = re.compile(r"[0-9a-f]{64}")


def record_values(record):
    """Serialize the loaded state, including file names without opening files."""
    return {
        field.attname: field.value_to_string(record) if field.get_internal_type() in {"FileField", "ImageField"}
        else getattr(record, field.attname)
        for field in record._meta.concrete_fields
    }


def require_outside_transaction():
    if connection.in_atomic_block or not connection.get_autocommit():
        raise ValidationError("Preverjanje assetov mora začeti zunaj transakcije.")


def _state(company, issue_date):
    from archive.models import ArchiveConfiguration

    company = Company.objects.get(pk=company.pk)
    signatory = izberi_veljavnega_podpisnika(company, issue_date)
    fingerprint = _kanonicni_fingerprint({
        "company": record_values(company),
        "signatory": record_values(signatory),
        "archive": list(ArchiveConfiguration.objects.order_by("pk").values()),
        "setup": list(InitialSetupCompletion.objects.order_by("pk").values()),
        "templates": [settings.PDF_TEMPLATE_VERSION, settings.ST_PDF_TEMPLATE_VERSION],
        "eslog": settings.FEATURE_ESLOG,
    })
    return company, signatory, fingerprint


@dataclass(frozen=True)
class IssuanceAssets:
    """Only the signed payload is authoritative; a dictionary is never a proof."""

    seal: str

    def payload(self):
        try:
            return signing.loads(self.seal, salt=_SALT)
        except (signing.BadSignature, TypeError, ValueError) as error:
            raise ValidationError("Dokazilo assetov ni veljavno.") from error

    @property
    def references(self):
        return self.payload()["references"]

    def __getitem__(self, name):
        try:
            return self.references[name]
        except KeyError as error:
            raise ValidationError("Vir asseta se je med preverjanjem spremenil.") from error


def prepare_issuance_assets(company, issue_date, *, frozen_paths=()):
    require_outside_transaction()
    company, signatory, fingerprint = _state(company, issue_date)
    references = {}
    for path, checksum in (
        (company.logotip.name, company.logotip_checksum),
        (signatory.podpis_slika.name, signatory.podpis_checksum),
    ):
        if not checksum or not _DIGEST.fullmatch(checksum):
            raise ValidationError("Manjka veljavna kontrolna vsota logotipa ali podpisa.")
        immutable = shrani_nespremenljiv_asset(path)
        if immutable != f"nespremenljivi-asseti/{checksum}":
            raise ValidationError("Vsebina logotipa ali podpisa se ne ujema s preverjenim vzorcem.")
        references[path] = immutable
    frozen_paths = tuple(sorted(set(frozen_paths)))
    for path in frozen_paths:
        digest = path.removeprefix("nespremenljivi-asseti/")
        if path != f"nespremenljivi-asseti/{digest}" or not _DIGEST.fullmatch(digest):
            raise ValidationError("Neveljavna zamrznjena pot asseta.")
        with default_storage.open(path, "rb") as source:
            if sha256(source.read()).hexdigest() != digest:
                raise ValidationError("Zamrznjeni asset je poškodovan.")
    return IssuanceAssets(signing.dumps({
        "company": company.pk, "date": issue_date.isoformat(),
        "signatory": signatory.pk,
        "logo": [company.logotip.name, company.logotip_checksum],
        "signature": [signatory.podpis_slika.name, signatory.podpis_checksum],
        "fingerprint": fingerprint, "references": references, "frozen_paths": frozen_paths,
    }, salt=_SALT))


def validate_issuance_assets(company, issue_date, evidence, *, frozen_paths=()):
    """Database and small metadata only; safe under SQLite IMMEDIATE."""
    if type(evidence) is not IssuanceAssets:
        raise ValidationError("Manjka preverjeno dokazilo assetov pred številčenjem.")
    payload = evidence.payload()
    company, signatory, fingerprint = _state(company, issue_date)
    expected = {
        company.logotip.name: f"nespremenljivi-asseti/{company.logotip_checksum}",
        signatory.podpis_slika.name: f"nespremenljivi-asseti/{signatory.podpis_checksum}",
    }
    if (payload.get("company") != company.pk or payload.get("date") != issue_date.isoformat()
            or payload.get("signatory") != signatory.pk
            or payload.get("logo") != [company.logotip.name, company.logotip_checksum]
            or payload.get("signature") != [signatory.podpis_slika.name, signatory.podpis_checksum]
            or payload.get("fingerprint") != fingerprint
            or payload.get("references") != expected
            or not set(frozen_paths).issubset(payload.get("frozen_paths", []))):
        raise ValidationError("Dokazilo assetov je zastarelo ali neskladno.")
    return expected
