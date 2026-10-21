"""Real setup evidence for tests of established issuance workflows."""

from calendar import monthrange
from pathlib import Path

from django.conf import settings
from django.test import override_settings
from django.utils import timezone

from archive.configuration import _verify_root, validate_archive_locations
from archive.models import ArchiveConfiguration
from companies import services
from companies.models import (
    Company,
    InitialSetupCompletion,
    NumberingVerification,
    NumberSequence,
    Signatory,
)
from companies.readiness import complete_initial_setup, verify_numbering
from contracts.models import Lease, LeaseItem, Property, Unit
from core.validators import preveri_davcno_stevilko
from rendering.services import render_sample_pdf
from tests.financial_fixtures import update_financial_fixture
from tests.smtp_helpers import approve_smtp


def preview_sample(company, lease):
    today = services.date.today()
    with override_settings(
        RENDERING_ROOT=Path(settings.MEDIA_ROOT) / "sample-previews"
    ):
        return render_sample_pdf(
            lease,
            datum_izdaje=today,
            obdobje_od=today.replace(day=1),
            obdobje_do=today.replace(day=monthrange(today.year, today.month)[1]),
        )


def approve_sample(company, lease):
    preview = preview_sample(company, lease)
    return services.potrdi_vzorec_podjetja(
        company, lease, preview_id=preview.preview_id
    )


def verified_archive_configuration():
    # Isolated TestCase transactions contain real filesystem probes, not mock evidence.
    base = Path(settings.MEDIA_ROOT)
    paths = [
        base.parent / f"{base.name}-setup-{name}"
        for name in ("local", "icloud", "dropbox")
    ]
    for path, label in zip(paths, ("Lokalni arhiv", "iCloud arhiv", "Dropbox mapa")):
        path.mkdir(exist_ok=True)
        _verify_root(path, label)
    roots = validate_archive_locations(*paths)
    values = {
        name: str(getattr(roots, name))
        for name in ("local_root", "icloud_root", "dropbox_root")
    }
    config, _ = ArchiveConfiguration.objects.update_or_create(
        pk=1,
        defaults={
            **values,
            "verified_at": timezone.now(),
            "verification_evidence": {
                **values,
                "operations": ["write", "read", "delete"],
            },
        },
    )
    return config


def add_setup_issuers(company, lease, *, count=4):
    issuers = []
    for index in range(count):
        values = {
            field.name: getattr(company, field.name)
            for field in Company._meta.fields
            if field.name != "id"
        }
        tax = next(
            str(number)
            for number in range(20000000 + index * 1000, 20001000 + index * 1000)
            if preveri_davcno_stevilko(str(number))
        )
        values.update(
            naziv=f"Izdajatelj {index + 2}", davcna_stevilka=tax, aktivno=True
        )
        issuer = Company.objects.create(**values)
        approve_smtp(issuer.pk)
        signatory = Signatory.objects.filter(company=company).first()
        Signatory.objects.create(
            company=issuer,
            ime_priimek=signatory.ime_priimek,
            podpis_slika=signatory.podpis_slika.name,
            velja_od=signatory.velja_od,
        )
        property_ = Property.objects.create(
            company=issuer, naziv=f"Stavba {index + 2}", naslov="Glavna 1"
        )
        unit = Unit.objects.create(
            property=property_, oznaka="P1", tip_enote=Unit.TipEnote.PISARNA
        )
        sample_lease = Lease.objects.create(
            unit=unit,
            tenant=lease.tenant,
            oznaka_pogodbe=f"VZ-{index + 2}",
            tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
            datum_zacetka=lease.datum_zacetka,
            rok_placila_dni=15,
        )
        source = lease.leaseitem_set.filter(arhivirana=False).first()
        LeaseItem.objects.create(
            lease=sample_lease,
            naziv_postavke="Najemnina",
            znesek_neto_mesecno="100.00",
            vat_regime=source.vat_regime,
            referenca_listine=source.referenca_listine,
            datum_reference=source.datum_reference,
        )
        services.potrdi_ddv_nastavitve(issuer)
        approve_sample(issuer, sample_lease)
        verify_numbering(
            issuer,
            np_last=0,
            st_last=0,
            st_year=timezone.localdate().year,
            explicit=True,
        )
        issuers.append(issuer)
    return issuers


def prepare_initial_setup(company, lease):
    """Complete all five issuers once, then archive auxiliary test issuers."""
    if InitialSetupCompletion.objects.exists():
        return
    defaults = {
        "MARJAN_SMTP_HOST": "smtp.example.test",
        "MARJAN_SMTP_PORT": 587,
        "MARJAN_SMTP_SECURITY": "STARTTLS",
        "MARJAN_SMTP_TIMEOUT_SECONDS": 10,
        "MARJAN_KEYRING_SERVICE": "test-marjanapp",
    }
    for name, value in defaults.items():
        if not getattr(settings, name, None):
            setattr(settings, name, value)
    services.potrdi_ddv_nastavitve(company)
    approve_sample(company, lease)
    counters = dict(
        NumberSequence.objects.filter(company=company).values_list(
            "tip", "zadnja_stevilka"
        )
    )
    from invoicing.models import Invoice

    # These values come from known synthetic invoices in the test fixture.
    historical = [
        number.rsplit("-", 1)[-1]
        for number in Invoice.objects.filter(
            company=company, stevilka__startswith="NP-"
        ).values_list("stevilka", flat=True)
    ]
    counters["NP"] = max(
        [
            counters.get("NP", 0),
            *(
                int(number)
                for number in historical
                if number.isascii() and number.isdigit()
            ),
        ]
    )
    verify_numbering(
        company,
        np_last=counters.get("NP", 0),
        st_last=counters.get("ST", 0),
        st_year=timezone.localdate().year,
        explicit=True,
    )
    others = add_setup_issuers(company, lease)
    approve_smtp(company.pk)
    if not ArchiveConfiguration.objects.exists():
        verified_archive_configuration()
    complete_initial_setup()
    update_financial_fixture(Company.objects.filter(pk__in=[issuer.pk for issuer in others]),
        aktivno=False
    )


def initial_np_last(company):
    return NumberingVerification.objects.filter(company=company).latest("pk").np_last


def first_np_number(company):
    return f"NP-{initial_np_last(company) + 1:05d}"


def allocate_verified_number(company, tip, datum_izdaje, *, asset_evidence=None):
    """Exercise the domain allocator with real asset verification before its lock."""
    from companies.issuance_assets import prepare_issuance_assets

    if asset_evidence is None:
        asset_evidence = prepare_issuance_assets(company, datum_izdaje)
    return services.dodeli_naslednjo_stevilko(
        company, tip, datum_izdaje, asset_evidence=asset_evidence,
    )
