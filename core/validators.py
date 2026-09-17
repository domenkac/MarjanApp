import re

_IBAN_SPECS = """
AD24 AE23 AL28 AT20 AZ28 BA20 BE16 BG22 BH22 BR29 BY28
CH21 CR22 CY28 CZ24 DE22 DK18 DO28 EE20 EG29 ES24 FI18
FO18 FR27 GB22 GE22 GI23 GL18 GR27 GT28 HR21 HU28 IE22
IL23 IQ23 IS26 IT27 JO30 KZ20 LB28 LC32 LI21 LT20 LU20
LV21 MC27 MD24 ME22 MK19 MR27 MT31 MU30 NL18 NO15 OM23
PK24 PL28 PS29 PT25 QA29 RO24 RS22 SA24 SC31 SE24 SI19
SK24 SM27 ST25 SV28 TL23 TN24 TR26 UA29 VA22 VG24 XK20
""".split()  # noqa: SIM905

IBAN_LENGTHS = {item[:2]: int(item[2:]) for item in _IBAN_SPECS}


def preveri_davcno_stevilko(stevilka: str) -> bool:
    if not isinstance(stevilka, str) or not re.fullmatch(r"[0-9]{8}", stevilka):
        return False
    cifre = [int(c) for c in stevilka]
    ponderji = [8, 7, 6, 5, 4, 3, 2]
    vsota = sum(c * p for c, p in zip(cifre[:7], ponderji))
    ostanek = vsota % 11
    if ostanek == 0:
        return False
    kontrolna = 11 - ostanek
    if kontrolna == 10:
        kontrolna = 0
    return kontrolna == cifre[7]


def preveri_maticno_stevilko(stevilka: str) -> bool:
    return isinstance(stevilka, str) and re.fullmatch(r"[0-9]{10}", stevilka) is not None


def normaliziraj_iban(iban: str) -> str:
    return "".join(iban.split()).upper()


def preveri_iban(iban: str) -> bool:
    if not isinstance(iban, str) or not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]+", iban):
        return False
    if len(iban) != IBAN_LENGTHS.get(iban[:2]):
        return False
    prestavljeno = iban[4:] + iban[:4]
    stevilsko = "".join(znak if znak.isdigit() else str(ord(znak) - 55) for znak in prestavljeno)
    return int(stevilsko) % 97 == 1
