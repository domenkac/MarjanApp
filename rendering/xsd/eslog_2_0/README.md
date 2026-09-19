# eSLOG 2.0 XSD

- Uradni vir paketa: https://epos.si/assets/docs/e-SLOG-2.0-08-2020-SL.zip
- Uradni vir računske sheme: https://epos.si/assets/docs/eSLOG20_INVOIC_v200.xsd
- Uradni vir XMLDSig sheme: https://epos.si/assets/docs/xmldsig-core-schema.xsd
- Različica: eSLOG 2.0, dokumentacija avgust 2020, XSD `v200`
- Datum pridobitve: 2026-09-19

Datoteki XSD sta originalni in nespremenjeni. Njuna SHA-256 odtisa se ujemata z datotekama v uradnem paketu.

## Potrjene preslikave za Fazo 2

### Kupec, ki je fizična oseba

eSLOG ne določa ločenega tipa kupca za fizično osebo. Kupec se zapiše kot `G_SG2`, kjer ima `S_NAD/D_3035` vrednost `BY`.

- naziv kupca: BT-44, `S_NAD/C_C080/D_3036`;
- naslov: BT-50–BT-55 v `S_NAD`;
- identifikator kupca BT-46, uradni registracijski identifikator BT-47 in identifikator za DDV BT-48 so neobvezni;
- za kupca, ki ni identificiran za DDV, uradno pojasnilo z dne 25. 11. 2020 določa davčno številko v NBT-013: `G_SG3/S_RFF/C_C506/D_1153 = AHP`, številka pa v `D_1154`.

Vir: uradni paket, *e-SLOG 2.0 Elektronski račun – 2. del: Sestava dokumenta*, strani 59–60, 66–68 in 199–201; https://www.epos.si/novice/pojasnilo-zapisa-davcnih-stevilk-prodajalca-in-kupca-v-e-slog-2-0-e-racun

Fizična oseba brez identifikatorja za DDV in brez matične številke je podprta, če ima davčno številko NBT-013/AHP. Veljaven zapis tudi brez davčne številke v uradni dokumentaciji ni določen in ostaja `UNRESOLVED`.

### Dobropis in sklic na izvirni račun

- račun: BT-3, `S_BGM/C_C002/D_1001 = 380`;
- dobropis: BT-3, `S_BGM/C_C002/D_1001 = 381`;
- številka izvirnega računa: BT-25, `G_SG1[S_RFF/C_C506/D_1153='OI']/S_RFF/C_C506/D_1154`;
- datum izvirnega računa: BT-26, `G_SG1[S_RFF/C_C506/D_1153='OI']/S_DTM/C_C507/D_2005 = 384`, datum v `D_2380`.

Vir: uradni paket, *1. del: Splošna navodila*, §8.1–8.3, stran 16; *2. del: Sestava dokumenta*, strani 30, 56–57 in 197–198; `sample_eSLOG20_INVOIC_v200_with_BT.xml` za BT-25/BT-26.

Ločena koda ali uradna preslikava za `STORNO` v pregledanem paketu ni določena. `STORNO` ostaja `UNRESOLVED`; ne preslika se samodejno v dobropis 381.

### Referenca pogodbe

`oznaka_pogodbe` se preslika v BT-12:

```text
/Invoice/M_INVOIC/G_SG1[S_RFF/C_C506/D_1153='CT']/S_RFF/C_C506/D_1154
```

Vir: uradni paket, *2. del: Sestava dokumenta*, strani 49 in 196; `sample_eSLOG20_INVOIC_v200_with_BT.xml`.

## Generiranje in dostava

Za vsak račun se XML generira, validira in arhivira. Prepoved kanala `ESLOG` za fizično osebo omejuje dostavo, ne generiranja XML.

## Poznejša uradna pojasnila

- Pojasnilo z dne 25. 11. 2020 dopolni zapis davčnih številk z `AHP`.
- Pojasnilo davčnih kod iz decembra 2024 ne spremeni zgornjih preslikav.
- Dopolnjeno pojasnilo iz avgusta 2026 določa zapis članov skupine za DDV. Velja samo, kadar je prodajalec ali kupec član skupine za DDV: https://epos.si/assets/images/Pojasnilo-zapisa-podatkov-o-skupini-za-DDV-v-eSLOG-2.0.pdf

EPOS še vedno označuje dokumentacijo računa iz avgusta 2020 kot zadnjo različico: https://epos.si/eslog
