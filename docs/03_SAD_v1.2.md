# SAD — Arhitekturni načrt

**Projekt:** Aplikacija za izdajanje najemniških računov
**Različica:** 1.2 · **Datum:** 16. september 2026 · **Status:** za potrditev naročnika · **Podlaga:** PRD v1.2 in FSD faze 1 v1.2 (osnutek) · **Namen:** skelet projekta za razvijalca — struktura, plasti, konvencije, vzorci

**Nadomešča:** SAD v1.1. Različica 1.2 nadomešča v1.1; v1.1 ostaja nespremenjena kot prejšnja potrjena različica.

**Spremembe v1.2:** arhitektura je razširjena z odgovornostmi čarovnika prvega zagona, varnim vzorčnim izrisom brez izdaje, obveznim neuničujočim preizkusom vseh treh lokacij shranjevanja in ločenima uporabnikovima potrditvama DDV-nastavitev ter vzorca; atomsko številčenje na Django 5.2 + SQLite uporablja kratke transakcije `IMMEDIATE`, ne `select_for_update()`; računovodja nima potrjevalne ali blokirne vloge in ostaja samo možni prejemnik BCC ter kontrolnega dokumenta; življenjski cikel začetnih podatkov uporablja domenske varovalke pred fizičnim in kaskadnim izbrisom, strukturirane razloge za zavrnitev ter minimalno revizijsko sled.

**Spremembe v1.1:** fizična struktura je usklajena s standardnimi Django app-i; `RecipientContact` je v `contracts`; Δ primerja zadnji dejansko izdani račun; vsak račun se potrjuje atomarno zase; modeli zaporedij, indeksacije in migracije so usklajeni s FSD v1.1.
**Ciljna publika:** razvijalec (interni sodelavec, junior, dela z ChatGPT/Codex)
**Spremljajoča datoteka:** `AGENTS.md` v korenu repozitorija (§9)

---

## 1. Odločitev o skladu

| Plast | Izbira | Zakaj (kratko — celotna razprava je bila sprejeta pred tem dokumentom) |
|---|---|---|
| Jezik | **Python 3.12+** | En jezik za cel projekt; razvijalec ga pozna |
| Ogrodje | **Django 5.2** | Vgrajen admin za velik del CRUD obsega, zrel ORM, podpora SQLite transakcijskemu načinu `IMMEDIATE` |
| Baza | **SQLite** | En uporabnik, ena naprava; ni razloga za PostgreSQL |
| UI vzorec | **Django templates + HTMX** | Dinamika brez ločenega JS ogrodja; en jezik ostane en jezik |
| PDF | **WeasyPrint** | HTML/CSS predloga → PDF; najbolje zastopan vzorec za rendering |
| eSLOG XML | **lxml** | Zrela, standardna knjižnica za XML in XSD validacijo |
| Poverilnice | **`keyring`** | Samodejno uporabi macOS Keychain, ena vrstica namesto ročne integracije |
| Namestitev | **`venv` neposredno na macOS** | Brez Dockerja (glej §8); brez notarizacije (ni distribucije tretjim) |
| Dostop | **Lokalni strežnik na `127.0.0.1`** | Brskalnik kot vmesnik; brez omrežne izpostavljenosti |

**Transakcijski način SQLite.** `DATABASES['default']['OPTIONS']['transaction_mode']` je nastavljen na `'IMMEDIATE'`. Vsak zunanji `transaction.atomic()` zato pridobi SQLite zaklep za pisanje ob začetku transakcije in prepreči, da bi dva pisca hkrati prebrala isto vrednost zaporedja. Transakcije, ki spreminjajo številčenje ali preverjajo reference pred fizičnim izbrisom, morajo ostati kratke in ne smejo vključevati generiranja datotek, omrežnih klicev ali drugega zunanjega dela. `select_for_update()` se za SQLite ne uporablja, ker ne zagotavlja vrstičnega zaklepanja.

**Kaj to ni:** ni prava spletna aplikacija (§4 PRD — en uporabnik, brez prijave), ni SaaS, ni multi-tenant. To je odločitev, sprejeta zavestno in je zapisana tudi v §2 tega dokumenta, da je ne pozabimo, ko projekt zraste.

---

## 2. Arhitekturni okvir in njegove meje

### 2.1 Kaj je in kaj ni "prava spletna aplikacija"

Django je spletno ogrodje, uporabljeno tu za enouporabniško, lokalno aplikacijo. To je zavestna odločitev (Django admin, učna zastopanost), ne privzeta izbira. Posledica:

| Ostane iz spletnega sveta | Zakaj kljub temu, da smo lokalni |
|---|---|
| CSRF zaščita | Django jo vklopi privzeto na vsakem POST obrazcu; izklop bi bil napačna "optimizacija" |
| Escape uporabniškega vnosa v predlogah (XSS) | Prosti vnos (naziv storitve, §8 PRD) gre skozi HTTP posrednik; brez ubežanja bi bila ranljivost resnična, četudi lokalna |
| ORM namesto raw SQL | Zaščita pred SQL injekcijo je stranski učinek, glavni razlog je vzdržljivost |

| Ne vrne se (izrecno izključeno) | Zakaj |
|---|---|
| Avtentikacija in avtorizacija (prijava, vloge) | En uporabnik, brez prijave; zaščita je na nivoju naprave, ne aplikacije |
| CORS | Ni zunanjih klicateljev |
| Rate limiting, Brute force zaščita | Ni napadalca na `127.0.0.1` |
| TLS/HTTPS | Promet ne zapusti naprave |
| SSRF | Aplikacija ne nalaga vsebine s tujih URL-jev |

**Če se to kdaj spremeni** (dostop z drugih naprav, drugi uporabniki) — glej §8 (Poti rasti). To ni nekaj, kar se zgodi mimogrede; zahteva zavestno odločitev in nov krog arhitekturnih vprašanj.

### 2.2 Plastenje — obvezna zahteva iz PRD §13

```
app/                 Django settings, URL usmerjanje, zagon
companies/           models.py (podatki) + services.py (domenska pravila)
catalogs/            models.py + services.py
contracts/           models.py + services.py
invoicing/           models.py + services.py
rendering/           PDF renderer (WeasyPrint), eSLOG XML renderer (lxml)
delivery/            e-pošta, čakalna vrsta, ponovni poskusi, keyring
accounting_export/   kontrolni dokument za računovodjo
archive/             arhiv, kontrolni odtisi, kopije, obnova
audit/               splošna revizijska sled v končnem izdelku
core/                skupni validatorji in pravila
ui/                  Django views, templates, forms, HTMX fragmenti
```

`models.py` in `services.py` sta plasti **znotraj vsake Django app mape**, ne ločeni vrhnji mapi. `models.py` vsebuje polja, relacije, migracije in osnovne poizvedbe; `services.py` oziroma namenski moduli v isti aplikaciji vsebujejo poslovna pravila.

**Pravilo, ki velja ne glede na Django konvencije:** domenske storitve se ne smejo sklicevati na `django.http`, `django.shortcuts` ali karkoli iz `ui/`. Njihova testabilnost brez zagnanega strežnika je merilo pravilnosti razdelitve — če testov ni mogoče pognati s `pytest` brez `runserver`, je razdelitev napačna.

To je v napetosti z Django konvencijo "fat models, thin views", kjer poslovna logika pogosto živi neposredno v modelih. Odločitev tega projekta: **modeli ostanejo tanki**, **poslovna logika pa živi v storitvah posamezne aplikacije** kot navadni Python razredi in funkcije. Razlog: poslovna pravila (DDV izračun, sorazmerni obračun, atomarno številčenje) morajo biti testabilna izolirano in razumljiva brez poznavanja Django views ali predlog.

---

## 3. Django aplikacije (moduli)

Django app je fizična in vsebinska meja za podatkovno ter domensko plast skupaj. Vsaka spodnja aplikacija je Django app v standardnem pomenu (`python manage.py startapp <ime>`), znotraj sebe pa loči tanek `models.py` od testabilnega `services.py` oziroma namenskih domenskih modulov.

| Django app | Odgovornost | Ključne entitete |
|---|---|---|
| `companies` | 5 izdajateljev, zaporedja, podpisniki ter stanji uporabnikovih potrditev DDV-nastavitev in vzorca | `Company`, `Signatory`, `NumberSequence`; natančna struktura potrditve vzorca se določi v FSD prvega zagona |
| `catalogs` | Šifranti — DDV režimi, storitve, enote mere | `VatRegime`, `ServiceCatalogItem` |
| `contracts` | Najemne pogodbe, enote, nepremičnine, najemniki in prejemniki | `Property`, `Unit`, `Tenant`, `Lease`, `LeaseItem`, `IndexationEvent`, `PoracunIndeksacije`, `RecipientContact` |
| `invoicing` | Jedro — računi, izračun, Δ izjeme in potrditev; pozneje tudi ročne storitvene fakture | `Invoice`, `InvoiceLine`, `MonthlyCycle` kot stanje skupnega postopka in `MonthlyBatch` kot stanje posameznega podjetja |
| `rendering` | PDF in eSLOG generiranje; ločen način vzorca z dejansko PDF-predlogo, vendar brez izdaje | brez lastnih modelov — bere potrjene račune iz `invoicing`, pri vzorcu pa preverjene podatke pogodbe |
| `delivery` | Pošiljanje e-pošte in dostavni poskusi | `DeliveryAttempt`; prejemnike bere iz `contracts.RecipientContact` |
| `accounting_export` | Kontrolni dokument za računovodjo; prejem nima potrjevalnega ali statusnega učinka | `AccountantReport` |
| `archive` | Dvotirni arhiv, kontrolni odtisi, Dropbox posnetki | `ArchivedFile`, `BackupSnapshot` |
| `audit` | Splošna revizijska sled v končnem izdelku, vključno z minimalnimi dogodki fizičnega izbrisa | razširi uporabo modela `core.AuditLogEntry` na vse zahtevane modele; sled izbrisa ne hrani celotne poslovne vsebine |
| `core` | Skupni validatorji, opravila na nadzorni plošči in konfiguracija | `AuditLogEntry` v ozkem obsegu faze 1 |

**Čarovnik prvega zagona.** `ui` vodi zaporedje korakov, vendar ne vsebuje poslovnih pravil. Storitve v `companies` in `contracts` preverijo popolnost podatkov, `archive` preveri dostopnost lokacij, `rendering` izdela označeni vzorec, `companies` pa hrani ločeni stanji uporabnikove potrditve DDV-nastavitev in uporabnikove potrditve vzorca. Čarovnik ne ustvari `Invoice`, ne kliče številčenja, pošiljanja ali uradnega arhiva.

Neuničujoči preizkus lokacij je odgovornost storitve v `archive/services.py`. Za lokalni arhiv, iCloud arhiv in Dropbox mapo storitev zapiše začasno datoteko, jo ponovno prebere in jo po uspešnem preverjanju odstrani. `ui` rezultat samo prikaže. Neuspeh katerekoli lokacije je blokada zaključka čarovnika in produkcijske izdaje. Ta začetna blokada ne spreminja pravila PRD §11.2 za kasnejšo začasno nedosegljivost iClouda in ne uvaja novega pravila za kasnejšo nedosegljivost Dropboxa.

**Zakaj `invoicing` in ne `contracts` vsebuje mesečni ciklus:** pogodba je vir podatkov, ne izvajalec postopka. `MonthlyCycle` hrani stanje enega skupnega šeststopenjskega uporabniškega postopka, `MonthlyBatch` pa je njegov otrok za posamezno podjetje. Hkrati je lahko v teku samo en `MonthlyCycle`, za isto obračunsko obdobje pa obstaja največ eden. Ob prvem začetku se v njegove `MonthlyBatch` zapise posnamejo takrat aktivna podjetja; poznejša aktivacija ali deaktivacija podjetja tega članstva ne spremeni.

**Opozorilo, ki je bilo že omenjeno v razpravi in ga tu formaliziramo:** Django admin (§5) je namenjen vzdrževanju matičnih podatkov v `companies`, `catalogs`, `contracts`. **Mesečni ciklus v `invoicing` ni admin CRUD** — je poslovni proces s stanji in prehodi, implementiran kot navadne Django views v `ui/`, ne kot admin akcije ali inline urejanje. Če se ciklus kdaj poskuša stlačiti v admin, ker je "na voljo", se izgubi nadzor nad zaporedjem korakov iz PRD §7.4.

---

## 4. Podatkovni model — pregled entitet

Podrobni modeli (polja, tipi, omejitve) so v FSD. Tu le entitete in razmerja, da se vidi celota.

```
Company (izdajatelj)                          5 zapisov
  ├── Signatory (podpisnik, z zgodovino; največ en trenutni zapis na podjetje zagotavlja pogojni DB UniqueConstraint)
  ├── NumberSequence (NP / ST; NP brez letnega ponastavljanja, ST po letu)
  ├── uporabnikova potrditev DDV-nastavitev (ločeno stanje)
  ├── uporabnikova potrditev vzorca (čas + različica PDF-predloge; podrobnosti v prihodnjem FSD)
  └── Property (nepremičnina)
        └── Unit (prostor / parkirno mesto)
              └── Lease (NAJEMNA POGODBA; veljavnost od–do)
                    ├── Tenant (najemnik — pravna ali fizična oseba)
                    ├── RecipientContact (TO/CC/BCC in kanal PDF/eSLOG/oba)
                    └── LeaseItem (postavka: znesek, VatRegime, veljavnost od–do)
                          ├── IndexationEvent (zgodovina uskladitev)
                          └── PoracunIndeksacije (zamujen vnos indeksa)

VatRegime (šifrant)                            urejljiv, z velja_od/velja_do
ServiceCatalogItem (šifrant)                   urejljiv, referenca na VatRegime

Invoice (RAČUN — NP ali ST)
  ├── InvoiceLine (POSNETEK; Lease FK je le sledljivost, ne vir vrednosti)
  ├── document_type: NP | ST | STORNO | DOBROPIS
  ├── number (dodeljen ob potrditvi, iz NumberSequence)
  ├── status (glej §7.5 PRD)
  ├── rendered_pdf_path, rendered_eslog_path, checksum
  └── DeliveryAttempt (0..n)

MonthlyCycle (skupni šeststopenjski mesečni postopek)
  ├── obdobje (prvi dan meseca; en cikel na obdobje)
  ├── datum_izdaje, trenutni_korak, status
  └── MonthlyBatch (n; posnetek podjetij ob začetku)
        └── company

AccountantReport (kontrolni dokument)
  ├── company, period
  ├── csv_path, pdf_path
  └── status (Odprto/Delno/Opozorilo/Zaključeno/Zastarelo)

ArchivedFile / BackupSnapshot
  └── checksum, locations (local/icloud/dropbox), verified_at

AuditLogEntry
  └── tip in ID zapisa kot neizsiljena sled, dejanje, field, old_value, new_value, reason, timestamp; pri fizičnem izbrisu brez celotne poslovne vsebine
```

**Načelo posnetka (PRD §6.1) je arhitekturno vozlišče celotnega modela.** `InvoiceLine` ne referencira `LeaseItem` s tujim ključem, ki bi se "spremenil, če se spremeni pogodba" — kopira vrednosti ob nastanku. Enako `Invoice` ne referencira žive `VatRegime` vrstice za stopnjo in klavzulo, ampak ju kopira. To je edino mesto v modelu, kjer se namerno **ne** uporabi normalizacija, in razlog je izrecno naveden v PRD (davčna zgodovinskost).

---

## 5. Django admin — obseg in meje

### 5.1 Kaj gre v admin

| App | Admin registracija | Prilagoditve |
|---|---|---|
| `companies` | `Company`, `Signatory`, `NumberSequence` | Inline za `Signatory` znotraj `Company`; `clean()` poda prijazne napake, pogojni DB `UniqueConstraint` pa zagotavlja največ en zapis z `velja_do IS NULL` na podjetje in enolično davčno številko med aktivnimi podjetji; upload polje za JPG/PNG s preverjanjem tipa po vsebini |
| `catalogs` | `VatRegime`, `ServiceCatalogItem` | Filter po `aktiven`; opozorilo v listi, če `velja_do` poteka |
| `contracts` | `Property`, `Unit`, `Tenant`, `Lease` | `Lease` z inline `LeaseItem`; `clean()` uporablja skupni validator iz `core` in storitve appa `contracts` |

### 5.2 Kaj NE gre v admin

- **Mesečni ciklus** (`MonthlyBatch`, potrditev, pošiljanje) — prilagojeni pogledi v appu `ui`, ne admin.
- **Ročne storitvene fakture** — custom view z izračunom nazaj iz bruto zneska (P0-17), ne admin dodajanje zapisa.
- **Kontrolni dokument** — sprožen prek opravila na nadzorni plošči, ne admin akcija.

### 5.3 Zakaj ta meja

Django admin je zgrajen za urejanje zapisov, ne za izvajanje procesov s stanji, izračuni in zunanjimi učinki (pošiljanje e-pošte, generiranje datotek). Poskus razširitve admina prek `admin.actions` za te namene vodi v skrito poslovno logiko znotraj admin plasti, kar krši ločitev iz §2.2 in naredi kodo netestabilno brez zagnanega admin vmesnika.

---

## 6. Ključni tokovi — sekvenčni prikaz

### 6.1 Mesečni ciklus (PRD §7.4)

```
Uporabnik → ui: »Nov mesečni paket«
              │
              ▼
    companies/services: preveri_predpogoje_podjetja()
              │  — veljavna uporabnikova potrditev DDV-nastavitev, veljavna potrditev vzorca in aktiven podpisnik
              │  — blokada samo prizadetega podjetja, ne vseh petih
              ▼
    invoicing/services: izracunaj_kandidate(mesec, podjetje)
              │  — deterministično iz Lease + vsake aktivne LeaseItem
              │  — upošteva začetek/konec pogodbe in posamezne postavke
              │  — ne piše v bazo
              ▼
    invoicing/services: Δ primerjava z zadnjim dejansko izdanim računom
              │  — preskočen mesec je ločen opozorilni signal
              │  — prvi produkcijski mesec uporablja sintetični migracijski račun
              ▼
    ui: skupni pregled in posamični predogled PDF
              │  — uporabnik vsak račun posebej označi ali odznači
              ▼
    ZA VSAK označeni račun POSEBEJ:
      transaction.atomic():
        1. v transakciji IMMEDIATE preberi in povečaj ustrezni NumberSequence
        2. ustvari Invoice + InvoiceLine kot nespremenljiv posnetek
        3. shrani številko in status `IZBRAN_ZA_POSILJANJE`
              │
              ├── rendering: PDF + eSLOG, kontrolni odtis
              ├── delivery: TO/CC/BCC; napaka omogoči ponovni poskus z istim PDF
              └── archive: lokalni arhiv + iCloud, preverba kontrolnega odtisa

    Neoznačeni kandidat → ARHIVIRAN_NI_IZDAN, brez številke
              ▼
    Zaključek: poslani / neizdani / neuspešni / arhivirani
```

**Kritična točka atomarnosti:** vsak potrjeni račun ima svojo `transaction.atomic()` transakcijo. Številka se dodeli šele ob potrditvi tega računa, zato prekinitev mesečnega postopka ne povzroči praznih številk in ne razveljavi že potrjenih računov. Generiranje datotek, pošiljanje in arhiviranje potekajo zunaj transakcije baze; stanje računa omogoča varno nadaljevanje. Paket je zato lahko delno zaključen, vendar je vsak posamezni račun notranje konsistenten.

### 6.2 Ročna storitvena faktura

```
Uporabnik → ui: »Nova storitvena faktura«
              │
              ▼
    Vnos: prejemnik, naziv (šifrant ali prost vnos), bruto znesek, DDV režim
              │
              ▼
    invoicing/services: izracunaj_iz_bruto(bruto, stopnja)
              │  — osnova + DDV = natanko bruto (P0-17)
              ▼
    Obvezen posamični pregled
              │
              ▼
    Potrditev v lastni transaction.atomic()
              │  — številka iz ST zaporedja za izbrano leto
              ▼
    rendering → delivery → archive
```

### 6.3 Storno / dobropis

```
Uporabnik → izbere obstoječi Invoice (poslan)
              │
              ▼
    invoicing/services: ustvari_storno(izvirni_racun, tip='STORNO'|'DOBROPIS')
              │  — nova Invoice vrstica, referenca na izvirnik
              │  — številka iz ISTEGA zaporedja kot izvirnik (NP ostane NP, ST ostane ST)
              │  — izvirnik se NE spremeni, NE izbriše
              ▼
    Enak tok pregleda, potrditve, pošiljanja kot §6.2
```

---

### 6.4 Prvi zagon in začetna nastavitev (PRD §16.4)

```
Uporabnik → ui: »Začni začetno nastavitev«
              │
              ▼
    companies/services + contracts/services:
      podjetje → nepremičnina → enota → najemnik → pogodba → postavke
              │  — strežniška validacija vseh obveznih podatkov
              │  — številčenje, lokacije, logotip, podpis in bančni podatki
              ▼
    archive/services: neuničujoče preveri vse 3 lokacije
              │  — lokalni arhiv, iCloud arhiv in Dropbox mapa
              │  — zapis, ponovno branje in odstranitev začasne datoteke
              │  — neuspeh katerekoli lokacije blokira zaključek
              ▼
    uporabnik potrdi DDV-nastavitve podjetja
              │  — ločeno shranjeno stanje potrditve
              ▼
    rendering: generiraj_vzorec(veljavna_pogodba, predloga)
              │  — dejanska oblika PDF-ja
              │  — oznaka »VZOREC – NI RAČUN«
              │  — brez Invoice, številke, delivery in uradnega archive zapisa
              ▼
    uporabnik pregleda in potrdi vzorec podjetja
              │  — ločeno: podjetje, čas, različica predloge
              ▼
    ponovi za vseh 5 podjetij
              ▼
    companies/services: preveri_predpogoje_za_produkcijsko_izdajo()
              │  — veljavni obe uporabnikovi potrditvi, uspešen preizkus vseh 3 lokacij
              │    in drugi obvezni pogoji
              ▼
    zaključek čarovnika; produkcijska izdaja je dovoljena
```

**Varnostna meja vzorca:** način vzorca uporablja isto produkcijsko PDF-predlogo, ne uporablja pa storitve za dodelitev številke in ne sme ustvariti ali spreminjati `Invoice`, `NumberSequence`, `DeliveryAttempt`, `ArchivedFile` ali evidence izdanih računov. To mora biti preverljivo s testi storitev.

**Zastarelost:** sprememba DDV-nastavitev razveljavi obe uporabnikovi potrditvi. Računovodsko pomembna sprememba ali sprememba PDF-predloge razveljavi potrditev vzorca; manjša vizualna sprememba ne razveljavi potrditve DDV. Natančna podatkovna struktura, stanja nadaljevanja čarovnika in razvrstitev sprememb sodijo v prihodnji FSD prvega zagona.

**Življenjski cikel začetnih podatkov:** vsak app, ki je lastnik poslovnega zapisa, izvede arhiviranje, ponovno aktivacijo in fizični izbris v svojem `services.py`. `ui` in Django admin ne kličeta `.delete()` neposredno. Skupni vmesnik storitve vrne strukturiran rezultat z najmanj `dovoljeno`, seznamom `razlogi` in `mozno_arhiviranje`; vsak razlog vsebuje stabilno kodo, človeku razumljivo sporočilo, vrsto povezave in število najdenih povezav.

Pred fizičnim izbrisom storitev v atomski transakciji ponovno preveri, da zapis še nikoli ni bil uporabljen in da nima poslovnih, podatkovnih ali podrejenih referenc. Poslovni tuji ključi do podjetij, nepremičnin, enot, najemnikov, pogodb, pogodbenih postavk in drugih zgodovinskih zapisov uporabljajo zaščitno obnašanje (`PROTECT` oziroma enakovredno), ne `CASCADE`. Obstoj kateregakoli podrejenega zapisa zavrne izbris; kaskadni fizični izbris poslovnega drevesa ni dovoljen.

Če je izbris zavrnjen, UI prikaže strukturirane razloge in kot primarno alternativo ponudi arhiviranje oziroma mehki izbris. Arhiviranje ohrani zapis in reference, izključi ga iz izbir za nove račune ter ne spremeni zgodovine. Ponovna aktivacija gre skozi domensko storitev in je dovoljena samo, če zapis še izpolnjuje veljavnost, referenčno celovitost in druga poslovna pravila.

Ob dovoljenem fizičnem izbrisu storitev zahteva signal izrecne uporabnikove potrditve ter v isti transakciji ustvari minimalni revizijski dogodek: vrsta in identifikator izbrisanega zapisa, čas ter dejanje `FIZICNI_IZBRIS`. Revizijski dogodek ne uporablja zaščitnega FK do izbrisanega zapisa in ne hrani njegovega celotnega poslovnega posnetka.

**Računovodja:** nima dostopa do aplikacije in ni udeleženec ali potrjevalec tega toka. BCC in kontrolni dokument sta izhodna prejemniška kanala brez vpliva na stanje računa, uspešnost mesečnega postopka ali dovoljenje za izdajo.

---

## 7. Presek s specifičnimi PRD zahtevami

Ta razdelek povezuje arhitekturne odločitve neposredno na P0 zahteve, da FSD ne začenja iz nič.

| PRD zahteva | Arhitekturna rešitev |
|---|---|
| P0-3 (deterministični izračun) | `izracunaj_osnutke()` je čista funkcija: enak vhod (stanje `Lease` v bazi) → enak izhod. Ne piše v bazo, dokler ni potrjeno. |
| P0-6 (zgodovinskost šifrantov) | `InvoiceLine` in `Invoice` hranita kopirane vrednosti, ne FK na `VatRegime`. Test: sprememba `VatRegime.stopnja` po izdaji ne sme spremeniti obstoječega `Invoice`. |
| P0-27 (nespremenljivost izdanega računa) | Po dodelitvi številke domenske storitve preprečijo spremembo poslovnih in posnetnih polj `Invoice`, vseh `InvoiceLine` ter izbris originala. Dovoljeni so le prehodi statusa, render poti/kontrolni odtis in evidenca pošiljanja prek namenskih storitev; popravek je storno ali dobropis. |
| P0-9 (validacija DDV po statusu prejemnika) | Skupni validator v `core`, klican iz `Lease.clean()` in iz storitve za pripravo kandidatov — ista pravila pri vnosu in izdaji. |
| P0-11 (sorazmernost postavke) | Vsaka `LeaseItem` ima lastna datuma začetka in prenehanja; izračun se opravi neodvisno za vsako vrstico. |
| P0-12/13 (ločeni zaporedji, resetiranje) | `NumberSequence(company, tip, leto, zadnja_stevilka)`: NP interno uporablja `leto=0` in se ne ponastavlja, ST uporablja konkretno koledarsko leto; baza uveljavi enoličnost in veljavne kombinacije tipa ter leta. |
| P0-14 (atomarno številčenje) | Django 5.2 uporablja SQLite `transaction_mode='IMMEDIATE'`; povečanje zaporedja in potrditev računa sta v isti kratki `transaction.atomic()` brez `select_for_update()`. DB `UniqueConstraint(company, stevilka)` prepreči podvojeno številko istega podjetja; test uporablja ločene povezave oziroma dejanske konkurenčne transakcije. |
| P0-16 (sorazmerni obračun) | Storitev v `invoicing/services.py` izračuna aktivne dni pogodbe in vsake postavke; testi pokrijejo 28/29/30/31 dni. |
| P0-18 (indeksacija) | `IndexationEvent` učinkuje praviloma 1. februarja; zamujen vnos ustvari uporabniško potrjene `PoracunIndeksacije` vrstice, ne spreminja izdanih računov. |
| P0-21 (eSLOG XSD validacija) | `rendering/eslog.py` generira in validira XML; napaka blokira kanal eSLOG v appu `delivery`. |
| P0-24 (Keychain) | `delivery/credentials.py` uporablja `keyring`; skrivnosti niso v bazi ali repozitoriju. |
| P0-25 (idempotentno pošiljanje) | `DeliveryAttempt` beleži vsak poskus; ponovni poskus uporabi isti PDF. |
| P0-29 (dvotirni arhiv) | `archive/services.py` zapiše lokalno in v iCloud ter primerja SHA-256. |
| P0-31 (Dropbox posnetki) | Periodična naloga v `archive/backup.py` izdela posnetek baze in arhiva; na macOS jo sproži `launchd`. |
| P0-33 (CSV slovenska lokalizacija) | `accounting_export/csv_renderer.py` uporablja podpičje, decimalno vejico in UTF-8 BOM. |
| P0-34 (opravilo na nadzorni plošči) | Pogled v `ui/dashboard.py` ob nalaganju izračuna stanje poročila za tekoči/pretekli mesec. |
| P0-37 (predpogoji podjetja) | `companies/services.py` pred pripravo preveri uporabnikovo `ddv_razporeditev_potrjena`, veljavno uporabnikovo potrditev vzorca in veljavnega `Signatory`; blokira samo prizadeto podjetje. Pogojni DB `UniqueConstraint` zagotavlja največ enega trenutnega podpisnika (`velja_do IS NULL`) na podjetje, `clean()` pa ohrani uporabniku prijazno validacijo. |
| P0-52 (čarovnik prvega zagona) | `ui` orkestrira korake, poslovna popolnost podatkov se preverja v storitvah `companies` in `contracts`, `archive/services.py` pa pred zaključkom neuničujoče preveri dejanski zapis in ponovno branje v lokalnem arhivu, iCloud arhivu in Dropbox mapi. Neuspeh katerekoli lokacije blokira zaključek in produkcijsko izdajo. |
| P0-53 (vzorčni račun) | `rendering` ima način vzorca z isto PDF-predlogo; ne ustvari `Invoice`, ne dodeli številke, ne pošilja in ne arhivira v uradni arhiv. |
| P0-54 (ločeni potrditvi) | `companies` hrani ločeni stanji in pravila zastarelosti; storitev pred izdajo preveri obe, brez zunanjega potrjevalca. |
| P0-55 (vzdrževanje podatkov) | Lastni app preveri izbris v domenski storitvi in vrne strukturirane razloge; poslovni FK-ji so zaščiteni pred `CASCADE`. Fizično se izbriše le nikoli uporabljen zapis brez referenc, sicer se ponudi arhiviranje; ponovna aktivacija se ponovno validira, dovoljen izbris pa pusti minimalni revizijski dogodek. |

---

## 8. Poti rasti — kaj arhitektura dopušča, ne da bi to zdaj gradili

Ta razdelek obstaja, da naslednja odločitev (če pride) ni presenečenje, in da se današnja arhitektura ne zapre nepotrebno.

| Če se pojavi potreba po... | Kaj to zahteva | Kaj v tej arhitekturi to omogoča brez predelave |
|---|---|---|
| Dostopu z drugih naprav (telefon, drug računalnik) v istem gospodinjstvu/pisarni | Tailscale ali podobno zasebno omrežje, **ne** javni strežnik | Django app ostane nespremenjena; spremeni se le `ALLOWED_HOSTS` in omrežna nastavitev. Avtentikacija/avtorizacija se **ne** vklopita — zasebno omrežje je nadomestek, ne dodatek. |
| Pravi spletni strežnik, javni dostop, drugi uporabniki | Vseh 10 varnostnih blokov nazaj (§2.1), PostgreSQL, avtentikacija, TLS, hosting | To je nov projekt v smislu obsega, ne "nadgradnja". §11 PRD (arhiv) bi bilo treba v celoti premisliti — SC5 (podatkovna suverenost) bi bil opuščen cilj. |
| eSLOG dejansko pošiljanje (P1) | Konfiguracija segmentov v UI, dejanska integracija s ponudnikom e-poti (P2) | `rendering/eslog.py` že generira validiran XML; `delivery` doda nov kanal brez predelave jedra. |
| Priključitev registra (AJPES, VIES) | Implementacija skupnega Python `Protocol` vmesnika iz PRD §6.9 | Validator v `core` že uporablja vmesnik, zato se ročni vir zamenja z registrom brez spremembe poslovnih pravil. |
| Docker | Postavitev na dejanski strežnik (glej vrstico "pravi spletni strežnik" zgoraj) | Ni potreben prej — glej sklep prejšnje razprave. Ko pride ta trenutek, je `Dockerfile` za Django standarden in dobro pokrit v učnih podatkih. |

---

## 9. AGENTS.md — vsebina za razvojno okolje

Datoteka v korenu repozitorija. Spodnja vsebina je usklajena s fizično strukturo appov iz §2.2 in z osnutkom FSD faze 1 v1.2.

```markdown
# AGENTS.md — Aplikacija za izdajanje najemniških računov

## Projekt
Django aplikacija za enega uporabnika, ki teče lokalno na 127.0.0.1.
SQLite baza. Viri zahtev: 01_PRD_v1.2.md, 03_SAD_v1.2.md in
04_FSD_Faza1_v1.2_FINAL.md.

## Ukazi
- Zagon: `python manage.py runserver`
- Testi: `pytest`
- Migracije: `python manage.py makemigrations && python manage.py migrate`
- Linting: `ruff check .`

## Arhitektura
- Django appi: `companies`, `catalogs`, `contracts`, `invoicing`,
  `rendering`, `delivery`, `accounting_export`, `archive`,
  `audit`, `core` in `ui`.
- Vsak app ima tanek `models.py` in testabilen `services.py`.
- Poslovna pravila niso v views, templates ali admin akcijah.
- Mesečni ciklus in ročne fakture so prilagojeni pogledi v `ui`,
  ne Django admin CRUD.

## Podatki
- Vsak izdani Invoice in InvoiceLine hranita nespremenljiv POSNETEK vrednosti.
- Žive LeaseItem/VatRegime reference niso vir vrednosti že izdanega računa.
- Sprememba šifranta ne sme vplivati na izdane račune.
- Po dodelitvi številke domenske storitve zavrnejo neposredno spremembo ali izbris vsebine računa in njegovih vrstic; brez DB triggerjev.
- Dodelitev številke je znotraj ločene `transaction.atomic()` za vsak račun.
- NP uporablja `NumberSequence.leto=0`; ST uporablja konkretno koledarsko leto.
- Januarski račun uporablja staro najemnino; indeks praviloma učinkuje 1. februarja.
- Zamujen indeks ustvari ločene vrstice poračuna in ne spreminja izdanih računov.

## Varnost lokalne aplikacije
- CSRF zaščita ostane vključena na vseh POST obrazcih.
- Ne uporabljaj `|safe` ali `mark_safe()` za uporabniško vsebino.
- Uporabljaj Django ORM; raw SQL je dovoljen le izjemoma v appu, ki je lastnik
  modela, z obrazložitvijo in testom.
- Ne dodajaj javnega gostovanja, avtentikacije, CORS, TLS ali rate limiting brez
  nove arhitekturne odločitve. Aplikacija je lokalna. Če meniš, da je kaj od tega
  potrebno, ustavi implementacijo in zahtevaj arhitekturno odločitev.
- SMTP poverilnice hrani prek `keyring`, nikoli v bazi, .env ali git.
- Druge nastavitve so v Django settings in se lahko berejo iz .env prek
  `django-environ`; skrivnosti ostanejo izključene.

## Zanesljivost
- Vsak zunanji klic ima timeout.
- Ponovni poskusi uporabljajo eksponentni zamik.
- Ponovni poskus pošiljanja uporablja isti PDF in ustvari DeliveryAttempt.
- Lokalni in iCloud arhiv preveri s SHA-256.
- Napake se zabeležijo in uporabniku prikažejo razumljivo.

## Poizvedbe
- Uporabi `select_related`/`prefetch_related` pri povezanih zapisih, kot je
  Lease → Tenant → Unit → Property.
- Paketni pregled približno 15–50 pogodb ne sme povzročiti N+1 poizvedb.

## Kakovost
- Zaklenjene verzije odvisnosti in Git od začetka.
- Testi za `services.py` so obvezni: DDV, številčenje, sorazmernost pogodbe
  in postavke, Δ proti zadnjemu izdanemu računu, indeksacija, poračun, veljavnost,
  migracijski sintetični račun in storno.
- Testi morajo dokazati zgodovinskost izdanih računov.
- Django admin je samo za vzdrževanje podatkov v `companies`, `catalogs` in
  `contracts`; mesečni ciklus in storitvene fakture ostanejo v `ui`.
```

---

## 10. Kaj ta dokument namerno izpušča

- **Natančne definicije polj, tipov, validacij in testov faze 1** — v FSD faze 1 v1.2 (osnutek).
- **UI postavitev in žični modeli** — v ločenem dokumentu UI/UX prototipov.
- **Podrobna stanja, podatkovni model potrditev in preslikava pravil življenjskega cikla na vsak model v čarovniku prvega zagona** — v prihodnjem FSD prvega zagona; pogoje izbrisa, arhiviranja in revizijske sledi določata PRD v1.2 §16.4 ter ta dokument.
- **Testni scenariji in kriteriji sprejemljivosti** — v Testnem načrtu.
- **eSLOG shema in preslikava polj** — v FSD za fazo 2, ko pride na vrsto.

Ta dokument odgovarja na »kje kaj živi in zakaj«. FSD faze 1 v1.2 je osnutek izvedbenega vira za jedro; prihodnji FSD prvega zagona bo podrobno opredelil čarovnik, testni načrt pa bo zahteve pretvoril v celovite sprejemne scenarije.
