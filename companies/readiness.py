"""Shared first-run validation and the atomic production numbering gate."""

from dataclasses import dataclass

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import override

from archive.configuration import ArchiveConfigurationError, validate_archive_locations
from archive.models import ArchiveConfiguration
from contracts.setup import lease_items_setup_errors, record_setup_errors
from core.models import AuditLogEntry
from delivery.configuration import (
    SmtpConfigurationError,
    resolve_keyring_credential_ref,
    resolve_smtp_config,
)

from .models import (
    Company,
    InitialSetupCompletion,
    NumberingVerification,
    NumberSequence,
)
from .services import (
    _kanonicni_fingerprint,
    veljavna_potrditev_ddv,
    veljavna_potrditev_vzorca,
)
from .setup import issuer_setup_issues


@dataclass(frozen=True)
class SetupReadiness:
    companies: list
    blockers: list
    archive_evidence: dict

    @property
    def ready(self):
        return not self.blockers and not any(row["issues"] for row in self.companies)

    @property
    def messages(self):
        return self.blockers + [
            f"{row['company'].naziv}: {message}"
            for row in self.companies
            for _, message in row["issues"]
        ]


def archive_verification_evidence(*, check_paths=True):
    config = ArchiveConfiguration.objects.filter(pk=1).first()
    if config is None:
        raise ArchiveConfigurationError("Lokacije shranjevanja še niso preverjene.")
    if check_paths:
        roots = validate_archive_locations(
            config.local_root, config.icloud_root, config.dropbox_root
        )
        paths = {
            name: str(getattr(roots, name))
            for name in ("local_root", "icloud_root", "dropbox_root")
        }
    else:
        # Durable verification survives later remote filesystem outages.
        paths = {
            name: getattr(config, name)
            for name in ("local_root", "icloud_root", "dropbox_root")
        }
    expected = {**paths, "operations": ["write", "read", "delete"]}
    if config.verification_evidence != expected:
        raise ArchiveConfigurationError(
            "Ponovno preverite vse tri lokacije shranjevanja."
        )
    return {**expected, "verified_at": config.verified_at.isoformat()}


def _numbering_audited(verification):
    return AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(NumberingVerification),
        object_id=verification.pk,
        polje="numbering",
        nova_vrednost=_kanonicni_fingerprint(
            {
                "np_last": verification.np_last,
                "st_last": verification.st_last,
                "st_year": verification.st_year,
            }
        ),
    ).exists()


def _numbering_issues(company, today):
    verification = (
        NumberingVerification.objects.filter(company=company).order_by("-pk").first()
    )
    if verification is None:
        return [
            ("numbering", "Izrecno preverite začetni vrednosti številčenja NP in ST.")
        ], None
    issues = []
    if not _numbering_audited(verification):
        issues.append(
            (
                "numbering",
                "Manjka revizijsko dokazilo preverjanja številčenja NP in ST.",
            )
        )
    for tip, year, minimum in (
        ("NP", 0, verification.np_last),
        ("ST", verification.st_year, verification.st_last),
    ):
        sequence = NumberSequence.objects.filter(
            company=company, tip=tip, leto=year
        ).first()
        if sequence is None or sequence.zadnja_stevilka < minimum:
            issues.append(
                (
                    "numbering",
                    f"Preverjena začetna vrednost {tip} se ne ujema s številčenjem.",
                )
            )
    if (
        not InitialSetupCompletion.objects.exists()
        and today.year != verification.st_year
    ):
        issues.append(
            (
                "numbering",
                "Za začetno nastavitev preverite začetno vrednost ST za tekoče leto.",
            )
        )
    return issues, verification


@override("sl")
def setup_readiness(*, today=None, company=None):
    today = today or timezone.localdate()
    blockers = []
    archive_evidence = {}
    if company is None:
        issuers = Company.objects.filter(aktivno=True).order_by("naziv", "pk")
        if issuers.count() < 5:
            blockers.append(
                "Začetna nastavitev zahteva najmanj pet dejansko konfiguriranih aktivnih podjetij."
            )
    else:
        issuers = Company.objects.filter(pk=company.pk, aktivno=True)
        if not issuers.exists():
            blockers.append("Podjetje ni aktivno in ne sme izdati novega računa.")
    try:
        archive_evidence = archive_verification_evidence(check_paths=company is None)
    except ArchiveConfigurationError as error:
        blockers.append(str(error))
    rows = []
    for issuer in issuers:
        issues = [
            issue
            for issue in issuer_setup_issues(issuer, today)
            if issue[0] != "numbering"
        ]
        numbering_issues, numbering = _numbering_issues(issuer, today)
        issues.extend(numbering_issues)
        try:
            resolve_smtp_config(issuer)
            resolve_keyring_credential_ref(issuer)
        except SmtpConfigurationError:
            issues.append(
                ("delivery", "Nastavitve dostave SMTP in Keychain niso popolne.")
            )
        ddv = veljavna_potrditev_ddv(issuer)
        sample = veljavna_potrditev_vzorca(issuer, require_preview=True)
        for state, field, label in (
            (ddv, "ddv", "Potrditev DDV"),
            (sample, "sample", "Potrditev vzorca"),
        ):
            if state.stanje != "valid":
                issues.append(
                    (
                        field,
                        f"{label} {'je zastarela' if state.stanje == 'stale' else 'manjka'}.",
                    )
                )
        if sample.potrditev:
            lease = sample.potrditev.lease
            for record in (lease.unit.property, lease.unit, lease.tenant, lease):
                issues.extend(
                    ("lease", message) for message in record_setup_errors(record)
                )
            issues.extend(
                ("items", message)
                for message in lease_items_setup_errors(lease, timezone.localdate())
            )
            for contact in lease.recipientcontact_set.filter(aktiven=True):
                issues.extend(
                    ("delivery", message) for message in record_setup_errors(contact)
                )
        else:
            issues.append(
                (
                    "lease",
                    "Izberite veljavno pogodbo s postavkami in potrdite njen PDF predogled.",
                )
            )
        rows.append(
            {
                "company": issuer,
                "ddv": ddv,
                "sample": sample,
                "numbering": numbering,
                "issues": issues,
            }
        )
    return SetupReadiness(rows, blockers, archive_evidence)


def _audit(record, field, value, reason):
    AuditLogEntry.objects.create(
        content_type=ContentType.objects.get_for_model(record),
        object_id=record.pk,
        polje=field,
        nova_vrednost=value,
        razlog=reason,
    )


@transaction.atomic
def verify_numbering(company, *, np_last, st_last, st_year, explicit=False):
    if (
        explicit is not True
        or any(
            type(value) is not int or value < 0 for value in (np_last, st_last, st_year)
        )
        or not 1 <= st_year <= 9999
    ):
        raise ValidationError(
            "Izrecno potrdite dejanski zadnji številki NP in ST ter leto ST."
        )
    for last in (np_last, st_last):
        try:
            NumberSequence._meta.get_field("zadnja_stevilka").run_validators(last)
        except ValidationError:
            raise ValidationError(
                "Začetna številka presega dovoljeno velikost številčenja."
            ) from None
    try:
        company = Company.objects.get(pk=company.pk, aktivno=True)
    except Company.DoesNotExist:
        raise ValidationError("Podjetje ni aktivno ali ne obstaja.") from None
    from invoicing.models import Invoice

    for tip, year, last in (("NP", 0, np_last), ("ST", st_year, st_last)):
        sequence = NumberSequence.objects.filter(
            company=company, tip=tip, leto=year
        ).first()
        prefix = "NP-" if tip == "NP" else f"ST-{year}-"
        used = Invoice.objects.filter(
            company=company, stevilka__startswith=prefix
        ).values_list("stevilka", flat=True)
        suffixes = [number.removeprefix(prefix) for number in used]
        if (sequence and last < sequence.zadnja_stevilka) or any(
            int(number) > last
            for number in suffixes
            if number.isascii() and number.isdigit()
        ):
            raise ValidationError(
                f"Začetna vrednost {tip} ne sme biti nižja od že uporabljene številke."
            )
        NumberSequence.objects.update_or_create(
            company=company, tip=tip, leto=year, defaults={"zadnja_stevilka": last}
        )
    verification = NumberingVerification.objects.create(
        company=company, np_last=np_last, st_last=st_last, st_year=st_year
    )
    _audit(
        verification,
        "numbering",
        _kanonicni_fingerprint(
            {"np_last": np_last, "st_last": st_last, "st_year": st_year}
        ),
        "Uporabnik je preveril začetni številki glede na zgodovino izdanih računov.",
    )
    return verification


@transaction.atomic
def complete_initial_setup():
    state = setup_readiness()
    if not state.ready:
        raise ValidationError(state.messages)
    evidence = {
        "archive": state.archive_evidence,
        "companies": [
            {
                "company_id": row["company"].pk,
                "vat_confirmation_id": row["ddv"].potrditev.pk,
                "sample_confirmation_id": row["sample"].potrditev.pk,
                "preview_id": str(row["sample"].potrditev.preview_id),
                "source_fingerprint": row["sample"].potrditev.source_fingerprint,
                "pdf_template_version": row["sample"].potrditev.pdf_template_version,
                "numbering_verification_id": row["numbering"].pk,
            }
            for row in state.companies
        ],
    }
    completion, created = InitialSetupCompletion.objects.get_or_create(
        pk=1, defaults={"evidence": evidence}
    )
    if created:
        _audit(
            completion,
            "initial_setup",
            _kanonicni_fingerprint(evidence),
            "Uporabnik je zaključil začetno nastavitev vseh izdajateljev.",
        )
    return completion


def initial_setup_completed():
    """Verify durable completion without rechecking mutable issuer readiness."""
    completion = InitialSetupCompletion.objects.filter(pk=1).first()
    return (
        completion is not None
        and AuditLogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(InitialSetupCompletion),
            object_id=completion.pk,
            polje="initial_setup",
            nova_vrednost=_kanonicni_fingerprint(completion.evidence),
        ).exists()
    )


def require_issuance_readiness(company, issue_date, *, tip=None):
    """Called inside SQLite IMMEDIATE at actual allocation; issued retries skip it."""
    if not initial_setup_completed():
        raise ValidationError(
            "Začetna nastavitev še ni zaključena. Produkcijska izdaja je blokirana."
        )
    # Initial setup is global; later changes invalidate the affected issuer.
    state = setup_readiness(today=issue_date, company=company)
    if not state.ready:
        raise ValidationError(state.messages)
    if tip == NumberSequence.Tip.ST:
        latest = NumberingVerification.objects.filter(company=company).latest("pk")
        if issue_date.year < latest.st_year:
            prior = (
                NumberingVerification.objects.filter(
                    company=company, st_year=issue_date.year
                )
                .order_by("-pk")
                .first()
            )
            sequence = NumberSequence.objects.filter(
                company=company, tip="ST", leto=issue_date.year
            ).first()
            if (
                prior is None
                or not _numbering_audited(prior)
                or sequence is None
                or sequence.zadnja_stevilka < prior.st_last
            ):
                raise ValidationError(
                    "Za zgodovinsko leto ST izrecno preverite začetno vrednost številčenja."
                )
    if tip in {NumberSequence.Tip.NP, NumberSequence.Tip.ST}:
        from invoicing.models import Invoice

        year = issue_date.year if tip == NumberSequence.Tip.ST else 0
        prefix = "NP-" if tip == NumberSequence.Tip.NP else f"ST-{year}-"
        sequence = NumberSequence.objects.filter(
            company=company, tip=tip, leto=year
        ).first()
        suffixes = [
            number.removeprefix(prefix)
            for number in Invoice.objects.filter(
                company=company,
                stevilka__startswith=prefix,
            ).values_list("stevilka", flat=True)
        ]
        last_used = max(
            (
                int(number)
                for number in suffixes
                if number.isascii() and number.isdigit()
            ),
            default=0,
        )
        if last_used > (sequence.zadnja_stevilka if sequence else 0):
            raise ValidationError(
                f"{company.naziv}: Preverite številčenje {tip}; števec je nižji od že izdane številke."
            )
