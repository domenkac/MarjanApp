# eSLOG 2.0 XSD

- Uradni vir paketa: https://epos.si/assets/docs/e-SLOG-2.0-08-2020-SL.zip
- Uradni vir računske sheme: https://epos.si/assets/docs/eSLOG20_INVOIC_v200.xsd
- Uradni vir XMLDSig sheme: https://epos.si/assets/docs/xmldsig-core-schema.xsd
- Različica: eSLOG 2.0, dokumentacija avgust 2020, XSD `v200`
- Datum pridobitve: 2026-09-19

Datoteki XSD sta originalni in nespremenjeni. Njuna SHA-256 odtisa se ujemata z datotekama v uradnem paketu.

SHA-256 uradnega paketa `e-SLOG-2.0-08-2020-SL.zip` je `95DD10D94B220071BCD6EEFD181ECB11E1DE75F22B54646B8D91ECEC0BBC6002`.

Za odločitev konfiguracije v1 so bili pregledani tudi naslednji uradni artefakti iz paketa; v tej nalogi niso kopirani v repozitorij in so določeni z uradnim virom ter SHA-256:

- *e-SLOG 2.0 Elektronski račun – 1. del: Splošna navodila*, avgust 2020, SHA-256 `346D0EACC68EEFE115D6704EE734EAA1B9ACDD4FC1E9D046E592CE57465729BD`;
- *e-SLOG 2.0 Elektronski račun – 2. del: Sestava dokumenta*, avgust 2020, SHA-256 `EBD16D20EE513380378BF625DC375DA2638B8E48202BB6F29722D4D46EE60940`;
- `sample_eSLOG20_INVOIC_v200.xml`, SHA-256 `CD909162E84FDD584E4A9BEB6ED83BF3FB64C864944D77D3E51D2D1EDECD6EE0`;
- `sample_eSLOG20_INVOIC_v200_with_BT.xml`, SHA-256 `2F33CB1C43878F4EC4FB165F40321F274A76BCF655C45741A8FAEF2F02A4127A`.

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

### Neobvezna konfiguracija v1

Konfiguracijska verzija `eslog-2.0-v200-config-v1` podpira samo dva segmentna ključa:

- `elektronski_naslov_kupca_vkljucen`: BT-49/BT-49-1 v kupčevem `G_SG5`, CTA `IC`, COM `EM`;
- `kontaktni_podatki_kupca_vkljuceni`: BG-9 v kupčevem `G_SG5`, CTA `PD`.

Oba Company defaulta sta `False`. Tenant lahko izrecno povozi posamezen segmentni ključ. Ni tretjega implicitnega default sloja.

Tenant optional fields v1 so:

- `elektronski_naslov_kupca`: BT-49, največ 512 znakov, COM `EM`;
- `kontaktna_tocka_kupca`: BT-56, največ 256 znakov;
- `kontaktni_telefon_kupca`: BT-57, največ 512 znakov, COM `TE`;
- `kontaktni_email_kupca`: BT-58, največ 512 znakov, COM `EM`.

Vir: *1. del*, §9.2, strani 17–18; *2. del*, segmenti 41–45, strani 72–76, in Dodatek A, strani 200–201; oba uradna XML primera.

BT-49 uporablja samo izrecno konfiguracijsko vrednost; `Tenant.email` ni nadomestni vir. BT-10 in BT-19 nista podprta v v1, ker njuno lastništvo Tenant/Lease/Invoice ni odločeno. `STORNO` in `DOBROPIS` ob lastni končni potrditvi razrešita trenutno Company/Tenant konfiguracijo, je ne kopirata iz izvirnika, nato pa ohranita svoj nespremenljiv snapshot.

Snapshot verzija v celoti zamrzne semantiko. Stari snapshot nikoli ne podeduje novih ključev ali prihodnjih defaultov.

## Generiranje in dostava

Za vsak račun se XML generira, validira in arhivira. Prepoved kanala `ESLOG` za fizično osebo omejuje dostavo, ne generiranja XML.

## Poznejša uradna pojasnila

- Pojasnilo z dne 25. 11. 2020 dopolni zapis davčnih številk z `AHP`.
- Pojasnilo davčnih kod iz decembra 2024 ne spremeni zgornjih preslikav.
- Dopolnjeno pojasnilo iz avgusta 2026 določa zapis članov skupine za DDV. Velja samo, kadar je prodajalec ali kupec član skupine za DDV: https://epos.si/assets/images/Pojasnilo-zapisa-podatkov-o-skupini-za-DDV-v-eSLOG-2.0.pdf

EPOS še vedno označuje dokumentacijo računa iz avgusta 2020 kot zadnjo različico: https://epos.si/eslog
