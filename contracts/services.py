from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from catalogs.models import VatRegime
from core.pravila import zaokrozi

from .models import Lease, LeaseItem


@dataclass(frozen=True)
class IzracunPostavke:
    osnova: Decimal
    ddv_stopnja: Decimal
    ddv_znesek: Decimal
    skupaj: Decimal
    ddv_nacin_obracuna: str
    ddv_klavzula: str
    ddv_rekap_skupina: str
    je_sorazmerno: bool


def dnevi_v_obdobju(
    obdobje_od: date,
    obdobje_do: date,
    entiteta_zacetek: date | None,
    entiteta_konec: date | None,
) -> int:
    ef_zacetek = max(obdobje_od, entiteta_zacetek) if entiteta_zacetek else obdobje_od
    ef_konec = min(obdobje_do, entiteta_konec) if entiteta_konec else obdobje_do
    if ef_zacetek > ef_konec:
        return 0
    return (ef_konec - ef_zacetek).days + 1


def efektivna_veljavnost_pogodbe(pogodba: Lease) -> tuple[date, date | None]:
    konec = (
        pogodba.datum_konca
        if pogodba.status == Lease.Status.AKTIVNA
        else pogodba.datum_prenehanja
    )
    return pogodba.datum_zacetka, konec


def efektivna_veljavnost_postavke(postavka: LeaseItem) -> tuple[date, date | None]:
    zacetek_pogodbe, konec_pogodbe = efektivna_veljavnost_pogodbe(postavka.lease)
    zacetek = max(zacetek_pogodbe, postavka.datum_zacetka_postavke or zacetek_pogodbe)
    konec_postavke = postavka.datum_prenehanja_postavke
    if konec_pogodbe and konec_postavke:
        konec = min(konec_pogodbe, konec_postavke)
    else:
        konec = konec_pogodbe or konec_postavke
    return zacetek, konec


def izracunaj_osnovo(
    znesek_neto_mesecno: Decimal,
    dnevi_najema: int,
    dnevi_v_mesecu: int,
) -> Decimal | None:
    if dnevi_najema == dnevi_v_mesecu:
        return znesek_neto_mesecno
    if dnevi_najema == 0:
        return None
    return zaokrozi(
        znesek_neto_mesecno * Decimal(dnevi_najema) / Decimal(dnevi_v_mesecu)
    )


def izracunaj_postavko(
    postavka: LeaseItem, obdobje_od: date, obdobje_do: date
) -> IzracunPostavke | None:
    pogodba = postavka.lease
    if (
        postavka.arhivirana
        or pogodba.arhivirana
        or not pogodba.tenant.aktiven
        or not pogodba.unit.aktivna
        or not pogodba.unit.property.aktivna
        or not pogodba.unit.property.company.aktivno
    ):
        return None

    zacetek, konec = efektivna_veljavnost_postavke(postavka)
    dnevi_najema = dnevi_v_obdobju(obdobje_od, obdobje_do, zacetek, konec)
    dnevi_v_mesecu = (obdobje_do - obdobje_od).days + 1
    osnova = izracunaj_osnovo(postavka.znesek_neto_mesecno, dnevi_najema, dnevi_v_mesecu)
    if osnova is None:
        return None

    rezim = postavka.vat_regime
    if rezim.nacin_obracuna == VatRegime.NacinObracuna.OBDAVCENO:
        ddv_znesek = zaokrozi(osnova * rezim.stopnja / Decimal(100))
    elif rezim.nacin_obracuna in {
        VatRegime.NacinObracuna.OPROSCENO,
        VatRegime.NacinObracuna.NI_PREDMET,
        VatRegime.NacinObracuna.OBRNJENA_DAVCNA,
    }:
        ddv_znesek = Decimal("0.00")
    else:
        raise ValueError(f"Neznan način obračuna DDV: {rezim.nacin_obracuna}")

    return IzracunPostavke(
        osnova=osnova,
        ddv_stopnja=rezim.stopnja,
        ddv_znesek=ddv_znesek,
        skupaj=osnova + ddv_znesek,
        ddv_nacin_obracuna=rezim.nacin_obracuna,
        ddv_klavzula=rezim.klavzula,
        ddv_rekap_skupina=rezim.rekapitulacijska_skupina,
        je_sorazmerno=dnevi_najema != dnevi_v_mesecu,
    )
