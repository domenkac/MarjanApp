"""Read-only issuer checks for first-run navigation, not an issuance gate."""

from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.core.files.storage import default_storage
from django.utils.translation import override

from .models import NumberSequence
from .services import izberi_veljavnega_podpisnika


@override("sl")
def issuer_setup_issues(company, today, *, asset_evidence=None):
    issues = []
    try:
        company.full_clean(exclude=["logotip"])
    except ValidationError as error:
        for field, messages in error.message_dict.items():
            label = {
                "trr": "IBAN",
                "email_posiljatelj": "E-pošta pošiljatelja",
                "email_racunovodja": "E-pošta računovodje",
            }.get(field, field.replace("_", " "))
            issues.extend((field, f"{label}: {message}") for message in messages)
    for field in company._meta.fields:
        value = getattr(company, field.name)
        if not field.blank and isinstance(value, str) and value and not value.strip():
            issues.append(
                (field.name, f"Manjka obvezen podatek: {field.verbose_name}.")
            )

    def asset_exists(name):
        # Number allocation supplies references from sealed preflight evidence.
        return name in asset_evidence if asset_evidence is not None else _asset_exists(name)

    if not asset_exists(company.logotip.name):
        issues.append(("logotip", "Logotip manjka ali ni dostopen."))
    try:
        signatory = izberi_veljavnega_podpisnika(company, today)
    except ValueError as error:
        issues.append(("signatory", str(error)))
    else:
        try:
            signatory.full_clean(exclude=["podpis_slika"])
        except ValidationError as error:
            issues.extend(("signatory", message) for message in error.messages)
        if not asset_exists(signatory.podpis_slika.name):
            issues.append(("signatory", "Slika podpisa manjka ali ni dostopna."))
    for tip, year in ((NumberSequence.Tip.NP, 0), (NumberSequence.Tip.ST, today.year)):
        if not NumberSequence.objects.filter(
            company=company,
            tip=tip,
            leto=year,
            zadnja_stevilka__gte=0,
        ).exists():
            issues.append(
                (
                    "numbering",
                    f"Manjka veljavno številčenje {tip} ({year or 'neprekinjeno'}).",
                )
            )
    return issues


def _asset_exists(name):
    if not name:
        return False
    try:
        return default_storage.exists(name)
    except (OSError, ValueError, SuspiciousFileOperation):
        return False
