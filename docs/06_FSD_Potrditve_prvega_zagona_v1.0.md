# FSD — stanji potrditev prvega zagona

**Različica:** 1.0 · **Podlaga:** PRD v1.2 §16.4 in SAD v1.2 §6.4

## Obseg

Dokument določa samo trajno stanje uporabnikove potrditve DDV-nastavitev in
uporabnikove potrditve vzorca podjetja. Ne določa uporabniškega vmesnika ali
poteka čarovnika.

## Podatkovni model

`VatSettingsConfirmation` je nespremenljiv zapis podjetja, časa potrditve,
SHA-256 fingerprinta in verzije fingerprint sheme.

`CompanySampleConfirmation` je nespremenljiv zapis podjetja, izbrane pogodbe,
časa potrditve, verzije PDF-predloge, SHA-256 fingerprinta in verzije
fingerprint sheme. Povezavi na podjetje in pogodbo uporabljata `PROTECT`.
Ponovna potrditev ustvari nov zapis; starega ne spremeni.

## Fingerprint in veljavnost

Fingerprint uporablja kanonični JSON z eksplicitno izbranimi semantičnimi
polji, stabilnim vrstnim redom, fiksnimi decimalnimi nizi, ISO datumi in
SHA-256. Čas potrditve ni del fingerprinta.

DDV-fingerprint pokriva status DDV podjetja, razporeditev ne-arhiviranih
pogodbenih postavk na DDV-režime in poslovna polja uporabljenih DDV-režimov.
Ne pokriva zneskov, nazivov, najemnikov, referenčnih vrednosti, IBAN-a ali
vizualnih assetov.

Polja `VatRegime.aktiven`, `velja_od` in `velja_do` niso del DDV-fingerprinta,
ker trenutni obračun z njimi ne določa DDV-obravnave. Če dobijo obračunsko
semantiko, je treba povečati verzijo fingerprint sheme in jih vključiti.

Fingerprint vzorca pokriva prikazane pravne in plačilne podatke podjetja,
trenutni DDV-fingerprint, izbrano nepremičnino, enoto, pogodbo, njene
obračunske postavke in reference, prikazane podatke najemnika, podpisnika,
vsebinski identiteti logotipa in podpisa ter `PDF_TEMPLATE_VERSION`.

Ob običajni zamenjavi slike `FileField` ustvari novo UUID-pot. Pot in ob
potrditvi preverjeni SHA-256 sta del fingerprinta. Sprememba bajtov na isti
poti mimo nadzorovanega aplikacijskega oziroma storage postopka je kršitev
integritete shrambe, ne podprta poslovna sprememba. Običajno preverjanje
veljavnosti zato ne bere datotek.

Veljavnost temelji izključno na trenutnem semantičnem stanju. Če se stanje
spremeni, je potrditev zastarela, dokler se fingerprint ne ujema. Če se stanje
pozneje povrne na popolnoma enako potrjeno stanje, postane ustrezna zgodovinska
potrditev ponovno veljavna. Sistem zato ne uporablja revizijskih števcev,
mutable zastavic ali trajne semantike »enkrat zastarelo, vedno zastarelo«.

## Domenske storitve

`companies.services` zagotavlja:

- `izracunaj_fingerprint_ddv(company)`;
- `potrdi_ddv_nastavitve(company)`;
- `veljavna_potrditev_ddv(company)`;
- `izracunaj_fingerprint_vzorca(company, lease)`;
- `potrdi_vzorec_podjetja(company, lease)`;
- `veljavna_potrditev_vzorca(company)`.

Preverjanje vrne stanje `missing`, `stale` ali `valid`. Potrditev vzorca ne
ustvari računa, vrstice računa, številke, dostave, uradnega arhiva ali eSLOG-a.

## Legacy prehod

`Company.ddv_razporeditev_potrjena` začasno ostane zaradi združljivosti.
Vrednost `True` se ne pretvori v `VatSettingsConfirmation` in sama ne dokazuje
veljavne potrditve. Novi domenski storitvi upoštevata samo immutable zapise.
