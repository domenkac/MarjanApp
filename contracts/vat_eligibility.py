"""Recipient eligibility shared by assignment, calculation and snapshot issuance."""

from django.core.exceptions import ValidationError


def preveri_ddv_omejitev(sifra, zahteva_polni_odbitek):
    if not isinstance(sifra, str) or not sifra.strip():
        raise ValidationError("Manjka šifra DDV režima v dokazilih o upravičenosti.")
    if type(zahteva_polni_odbitek) is not bool:
        raise ValidationError(
            f"DDV režim {sifra}: zahteva polnega odbitka ni izrecno določena."
        )
    if sifra == "OBDAVCENO_45" and zahteva_polni_odbitek is not True:
        raise ValidationError("DDV režim OBDAVCENO_45 mora zahtevati polni odbitek.")
    if sifra == "OBDAVCENO_STANDARD" and zahteva_polni_odbitek is not False:
        raise ValidationError("DDV režim OBDAVCENO_STANDARD ne sme zahtevati polnega odbitka.")


def preveri_upravicenost_prejemnika(
    *, sifra, zahteva_polni_odbitek, tip, identificiran_za_ddv,
    polni_odbitek, najemnik, postavka,
):
    context = f"Najemnik {najemnik}, postavka {postavka}, DDV režim {sifra}"
    try:
        preveri_ddv_omejitev(sifra, zahteva_polni_odbitek)
    except ValidationError as exc:
        raise ValidationError(f"{context}: {exc.messages[0]}") from exc
    if (
        tip not in {"PRAVNA_OSEBA", "FIZICNA_OSEBA"}
        or type(identificiran_za_ddv) is not bool
        or type(polni_odbitek) is not bool
    ):
        raise ValidationError(f"{context}: dokazila o statusu prejemnika so nepopolna.")
    if zahteva_polni_odbitek and not (
        tip == "PRAVNA_OSEBA"
        and identificiran_za_ddv is True
        and polni_odbitek is True
    ):
        raise ValidationError(
            f"{context}: zahtevan je pravni prejemnik, identificiran za DDV "
            "s polno pravico do odbitka."
        )


def preveri_postavko_ddv(postavka, *, tenant=None):
    tenant = tenant if tenant is not None else postavka.lease.tenant
    regime = postavka.vat_regime
    preveri_upravicenost_prejemnika(
        sifra=regime.sifra, zahteva_polni_odbitek=regime.zahteva_polni_odbitek,
        tip=tenant.tip, identificiran_za_ddv=tenant.identificiran_za_ddv,
        polni_odbitek=tenant.ddv_zavezanec_s_polnim_odbitkom,
        najemnik=tenant.naziv, postavka=f"{postavka.naziv_postavke} (#{postavka.pk})",
    )
