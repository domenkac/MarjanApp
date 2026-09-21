# Funkcionalna specifikacija — Faza 2 (v1.0 DRAFT)

**Aplikacija Marjan — najemniško zaračunavanje**  
**Različica:** 1.0 rev.2 · **Datum:** 18. september 2026 · **Status:** DRAFT  
**Podlaga:** PRD v1.2, SAD v1.2, FSD Faze 1 v1.2 FINAL  
**Obseg:** `rendering` — PDF + eSLOG 2.0 iz istega strukturiranega računa; vzorčni PDF način brez izdaje

> Ta dokument je izvedbeni osnutek. Kjer PRD/SAD/FSD1 zahteve niso dovolj natančne ali so med seboj v napetosti, je to izrecno označeno kot **ODPRTA ODLOČITEV** ali **ZUNANJI BLOCKER**. Takšne točke niso tiho dopolnjene z domnevami.

---

## 0. Namen in meja faze

Faza 2 doda ločen modul `rendering`, ki strukturiran račun pretvori v:

1. PDF prek **WeasyPrint**;
2. eSLOG 2.0 XML prek **lxml**;
3. vzorčni PDF za prvi zagon, ki uporablja isto produkcijsko PDF-predlogo, vendar ni izdan račun.

Faza 2 ne vsebuje UI mesečnega ciklusa, pošiljanja e-pošte, uradnega arhiva, iCloud/Dropbox zapisovanja, kontrolnega dokumenta za računovodjo ali dashboarda. Ti deli ostajajo v poznejših fazah.

**Rezultat faze po PRD:** PDF in eSLOG nastajata iz istega strukturiranega zapisa; za vsako od petih podjetij je mogoče izdelati dejanski vzorčni PDF za pregled v čarovniku.

### 0.1 Prioritete

- **MUST** — blocker za zaključek Faze 2.
- **SHOULD** — priporočljivo, vendar ni blocker.
- **OUT OF SCOPE** — zavestno prestavljeno v poznejšo fazo.
- **OPEN** — odločitev mora biti potrjena pred FINAL verzijo tega FSD.
- **EXTERNAL BLOCKER** — manjka zunanji uradni artefakt; implementacija zadevnega dela ne sme ugibati.

### 0.2 Potrjene odločitve pregleda 18. 9. 2026

Potrjeno:

- logotip izdajatelja se snapshotira na `Invoice`;
- verzija PDF predloge se snapshotira ob potrditvi/izdaji računa;
- `Invoice.checksum` pomeni SHA-256 finalnih PDF bytes;
- eSLOG dobi ločen `rendered_eslog_checksum`;
- eSLOG davčna koda se snapshotira na `InvoiceLine`;
- eSLOG se generira, validira in arhivira za vsak račun, tudi za prejemnika tipa `FIZICNA_OSEBA`; dostavni kanal `ESLOG` zanj ostaja prepovedan;
- manjkajoči PDF se sme rekonstruirati samo prek ločene recovery storitve in samo, če rezultat ustreza originalnemu checksumu.

Uradni eSLOG 2.0 XSD/mapping paket je pridobljen in verzioniran. Odprta ostaja preslikava fizične osebe brez davčne številke za 2B-2.

---

## 1. Arhitekturna pravila

### 1.1 En strukturiran račun, dva rendererja — MUST

PDF in eSLOG sta dva izhoda istega strukturiranega računa.

Za **izdani račun** renderer bere poslovno vsebino samo iz:

- `Invoice`;
- `InvoiceLine`;
- njihovih shranjenih snapshot vrednosti;
- render metapodatkov, ki jih Faza 2 izrecno doda.

Renderer ne sme ponovno računati zneskov ali davčnih pravil iz:

- `Lease`;
- `LeaseItem`;
- trenutnega `VatRegime`;
- trenutnega `Tenant`;
- trenutnih poslovnih vrednosti `Company`.

FK-ji na pogodbo in postavko so za sledljivost, ne vir zgodovinske vrednosti.

### 1.2 Renderer ni poslovni kalkulator — MUST

Renderer:

- ne spreminja `osnova`, `ddv_znesek`, `skupaj`;
- ne računa nove najemnine;
- ne izvaja indeksacije;
- ne izbira novega DDV režima;
- ne spreminja številke, statusa izdaje ali NumberSequence;
- sme iz snapshot vrstic izračunati le **prikazne agregate** (npr. DDV rekapitulacijo), pri čemer mora rezultat preveriti proti shranjenim skupnim vrednostim računa.

Če se vsota vrstic ne ujema s shranjenimi total-i računa, je to napaka integritete in renderiranje se zavrne. Renderer ne sme tiho “popraviti” podatkov.

### 1.3 Brez zunanjega dela v transakciji številčenja — MUST

Generiranje PDF/XML in datotečni I/O sta zunaj transakcije, ki dodeli številko. Če renderiranje po uspešni potrditvi spodleti:

- številka ostane porabljena;
- `Invoice` in `InvoiceLine` ostaneta nespremenjena;
- ponovni poskus dela na istem računu in isti številki.

### 1.4 `rendering` nima lastnih poslovnih modelov — MUST

`rendering` je ločen Django app/modul brez lastnega poslovnega vira resnice. Vsebuje predvsem:

```text
rendering/
    services.py
    pdf.py
    eslog.py
    contexts.py
    templates/
        rendering/invoice/
    xsd/
        ... uradni eSLOG artefakti ...
```

Natančna razdelitev datotek je izvedbena odločitev, vendar poslovna logika ne sme preiti v Django views ali template.

---

## 2. Obstoječi podatkovni vhod

### 2.1 `Invoice` — potrjeni snapshot

Faza 1 že zagotavlja najmanj:

- podjetje;
- tip dokumenta `NP | ST | STORNO | DOBROPIS`;
- številko po potrditvi;
- datum izdaje;
- obdobje storitve;
- rok plačila pri NP in ST;
- snapshot podatkov prejemnika;
- snapshot podatkov izdajatelja;
- snapshot IBAN;
- snapshot podpisnika;
- oznako pogodbe;
- naziv objekta;
- povezavo na izvirnik pri storno/dobropisu;
- razlog korekcije;
- skupno osnovo;
- skupni DDV;
- skupaj z DDV;
- `rendered_pdf_path`;
- `rendered_eslog_path`;
- `checksum`.

### 2.2 `InvoiceLine` — potrjene vrstice

Faza 1 zagotavlja najmanj:

- dobesedni `opis`;
- `osnova`;
- `ddv_stopnja`;
- `ddv_znesek`;
- `skupaj`;
- snapshot načina DDV obračuna;
- snapshot DDV klavzule;
- snapshot rekapitulacijske skupine;
- podatek o sorazmernosti;
- podatke o ročnem popravku, kadar obstajajo.

### 2.3 Manjkajoči snapshoti, pomembni za Fazo 2

#### 2.3.1 Logotip izdajatelja — POTRJENO

`Company.logotip` obstaja, `Invoice` pa v Fazi 1 ne hrani poti/verzije logotipa.

Zaradi načela zgodovinskosti Faza 2 doda na `Invoice`:

```text
izdajatelj_logotip_slika_pot
```

Vrednost se zapiše kot snapshot asseta, ki velja ob potrditvi/izdaji računa.

Vzorec uporablja trenutno veljaven, že validiran logotip podjetja; izdani račun uporablja samo snapshot.

Asset, na katerega kaže že izdani račun, se ne sme fizično prepisati ali odstraniti. Nova slika dobi novo varno shranjeno ime/verzijo, stara pa ostane na voljo zaradi zgodovinskosti in morebitnega recovery renderja.

Enako pravilo velja za **sliko podpisa** iz `Invoice.izdajatelj_podpisnik_slika_pot`:

- pot na izdanem računu kaže na nespremenljivo verzijo asseta;
- zamenjava logotipa ali podpisa nikoli ne prepiše bytes stare datoteke;
- nova slika se shrani kot nov varen/versioniran asset;
- asset, ki ga referencira katerikoli izdani račun, se ne sme fizično izbrisati;
- renderer pri izdanem računu nikoli ne pade nazaj na trenutni `Company.logotip` ali trenutni `Signatory` asset.

Praktična izvedba je lahko UUID/content-hash/versionirano ime datoteke; poslovna zahteva je stabilna identiteta in nespremenljivost bytes.

#### 2.3.2 Različica PDF predloge — POTRJENO

PRD zahteva sledenje različici predloge pri potrditvi vzorca, obstoječi `Invoice` pa ne hrani različice predloge.

Faza 2 doda:

```text
pdf_template_version
```

kot nespremenljiv render snapshot.

**Ključno pravilo:** `pdf_template_version` se določi in shrani **ob potrditvi/izdaji računa, v isti poslovni točki ko račun postane oštevilčen**, ne šele ob prvem renderju. Tako kasnejša sprememba predloge ne more vplivati na že izdani račun, tudi če se PDF zaradi napake ustvari šele pozneje.

Ponovni render/recovery mora uporabiti isto verzijo. Stare verzije produkcijske predloge se ne prepisujejo.

Polje po izdaji ni splošno uredljivo; nastavi ga samo predpisani issuance/render lifecycle.

#### 2.3.3 eSLOG davčna koda — POTRJENO

`VatRegime.eslog_koda` je v Fazi 1 namenoma neobvezna in se polni v Fazi 2. `InvoiceLine` trenutno ne hrani njenega snapshota.

Faza 2 doda na `InvoiceLine`:

```text
ddv_eslog_koda
```

kot snapshot.

Pri pripravi novih računov se vrednost kopira iz veljavnega `VatRegime.eslog_koda`. Za račune, pri katerih je eSLOG obvezen po tem FSD, se potrditev/izdaja oziroma eSLOG preflight zavrne, če zahtevana koda manjka.

Renderer nikoli ne bere trenutne `VatRegime.eslog_koda` za že izdani račun.

Točne dovoljene vrednosti kod se potrdijo po pridobitvi uradne eSLOG 2.0 sheme in šifrantov.

#### 2.3.4 Tip prejemnika za eSLOG dostavo — POTRJENO kot posledica O6

eSLOG se generira za vsak račun. Tip prejemnika določa samo dovoljenost dostavnega kanala `ESLOG`; te odločitve ni dovoljeno sprejeti iz živega `Tenant` zapisa.

Faza 2 zato doda na `Invoice` snapshot:

```text
prejemnik_tip
```

z najmanj vrednostma:

```text
PRAVNA_OSEBA
FIZICNA_OSEBA
```

Vrednost se določi ob pripravi/potrditvi dokumenta. Pri ročnih ST dokumentih jo mora podati vir podatkov prejemnika. Po izdaji je nespremenljiva.

Če je `prejemnik_tip == FIZICNA_OSEBA`:

- eSLOG se generira, validira in arhivira;
- dostavni kanal `ESLOG` ostane prepovedan;
- manjkajoča davčna številka ne blokira izdaje računa ali 2B-1;
- preslikava kupca brez davčne številke ostane `UNRESOLVED` za 2B-2; renderer ne sme izumiti identifikatorja ali placeholderja.

#### 2.3.5 eSLOG status DDV in država partnerja — POTRJENO

`Company.ddv_zavezanec` je avtoritativen vir statusa izdajatelja. `Tenant.identificiran_za_ddv` je ločen avtoritativen vir statusa prejemnika; `ddv_zavezanec_s_polnim_odbitkom` ni nadomestek. Pri pripravi NP osnutka se na `Invoice` skupaj z drugimi podatki partnerjev zapišejo nespremenljivi snapshoti `izdajatelj_identificiran_za_ddv` in `prejemnik_identificiran_za_ddv`. Potrditev jih ne osveži iz živih partnerjev.

`Company.drzava_koda` in `Tenant.drzava_koda` sta obvezni kodi ISO 3166-1 alpha-2. Pri pripravi NP osnutka se zapišeta v `Invoice.izdajatelj_drzava_koda` in `Invoice.prejemnik_drzava_koda`; potrditev ju ne osveži. Obstoječi zapisi se migrirajo na `SI`; novi partnerji nimajo privzete države. Obstoječi partnerji in računi se migrirajo na status identificiran za DDV.

Za partnerje s kodo države `SI` ostaneta obvezni stroga slovenska validacija davčne številke in, pri pravnih osebah, slovenski format matične številke. Tuja pravna oseba mora imeti neprazno davčno številko. Če je identificirana za DDV, mora imeti tudi izrecni polni VAT ID. Zanjo se slovenska kontrolna številka in slovenski format matične številke ne preverjata; tuji registracijski identifikator je lahko prazen ali dolg največ 70 znakov. Zunanje preverjanje VIES ni del v1.

`Tenant.ddv_identifikacijska_stevilka` hrani izrecni polni VAT ID tujega prejemnika in se pri pripravi osnutka kopira v `Invoice.prejemnik_ddv_identifikacijska_stevilka`. Za slovenskega prejemnika se `VA` deterministično sestavi kot `SI` + slovenska davčna številka. Za tujega DDV-identificiranega prejemnika se v `VA` zapiše nespremenjeni polni snapshot VAT ID; koda države se ne dodaja. `AHP` vedno vsebuje davčno registracijsko številko. Renderer zavrne DDV-identificiranega tujega prejemnika brez polnega VAT ID.

Podpora skupinam za DDV je zunaj obsega v1. Polj ali preslikav skupine se ne dodaja.
---

### 2.4 Modelne dopolnitve Faze 2 — povzetek

Faza 2 uvede naslednje dodatke:

**Invoice**
- `prejemnik_tip` — snapshot `PRAVNA_OSEBA` / `FIZICNA_OSEBA`;
- `izdajatelj_logotip_slika_pot` — snapshot;
- `pdf_template_version` — snapshot določen ob potrditvi/izdaji;
- `rendered_eslog_checksum` — SHA-256 finalnega XML.

**InvoiceLine**
- `ddv_eslog_koda` — snapshot.

Obstoječi `Invoice.checksum` ostane SHA-256 finalnih PDF bytes.

Za izdane račune so ta snapshot polja nespremenljiva; render poti in checksumi se nastavljajo samo prek namenskih storitev.

**Asset retention:** logotipi in podpisi, ki jih referencira izdani `Invoice`, so versionirani oziroma nadomestno shranjeni in se ne prepisujejo ali fizično brišejo. Snapshot poti mora zato identificirati konkretne nespremenljive bytes, ne zgolj »trenutno datoteko« podjetja/podpisnika.

---

## 3. Skupni render lifecycle

### 3.1 Kateri račun se sme finalno renderirati — MUST

Finalni PDF/XML se sme generirati samo za račun, ki:

- obstaja v bazi;
- ima `stevilka IS NOT NULL`;
- ima vse zahtevane snapshot podatke;
- ima vsaj eno `InvoiceLine`;
- ima shranjene totale;
- ni sintetični migracijski zapis `ARHIVIRAN_NI_IZDAN`.

Renderer ne sme številčiti računa.

### 3.2 Preflight — MUST

Pred pisanjem datoteke render storitev preveri:

1. številko in tip dokumenta;
2. obvezne snapshot podatke;
3. obstoj vrstic;
4. konsistentnost totalov;
5. obvezne klavzule za snapshot DDV načine;
6. obstoj dovoljenih lokalnih slik, če se uporabljajo;
7. eSLOG posebne predpogoje pri XML renderju.

Pri PDF preflightu se obveznost roka plačila razlikuje po tipu dokumenta, kot določa §6.3.

Napaka vrne jasno domensko/render napako in ne spremeni poslovnih podatkov računa.

### 3.3 Prvi render — MUST

Prvi finalni render:

1. prebere svež račun in vrstice;
2. naredi preflight;
3. sestavi neodvisen render context;
4. generira bytes;
5. izračuna checksum;
6. datoteko zapiše atomsko prek začasne datoteke + rename;
7. šele po uspešnem zapisu prek namenske storitve nastavi render metapodatke.

Nikoli se ne shrani pot do delno zapisane datoteke.

### 3.4 Ponovni klic rendererja — MUST

Če je finalni artifact že ustvarjen in njegov checksum ustreza:

- storitev vrne obstoječo datoteko;
- ne generira novega dokumenta;
- ne spreminja poti ali poslovnih podatkov.

To je osnovna idempotentnost Faze 2 in podlaga za kasnejši varen retry pošiljanja.

Če je pot shranjena, datoteka pa manjka ali checksum ne ustreza, storitev vrne napako integritete. Ne sme tiho ustvariti drugega PDF-ja in prepisati reference.

### 3.5 Rekonstrukcija manjkajoče datoteke — POTRJENO

PRD omenja, da je generiran PDF mogoče poustvariti iz baze, hkrati pa zahteva isti potrjeni PDF pri ponovnem pošiljanju.

Varna interpretacija:

- običajni retry vedno uporablja obstoječe bytes;
- posebna “rebuild” storitev je ločena od običajnega renderja;
- rebuild je dovoljen samo, če so na voljo ista verzija predloge in vsi snapshot asseti;
- rezultat se primerja s shranjenim checksumom;
- če se checksum razlikuje, rebuild ne sme samodejno nadomestiti originala.

To pravilo je potrjeno. Recovery ni običajen rerender in nikoli tiho ne zamenja kanoničnega artifacta.

---

## 4. Checksum semantika

### 4.1 Razrešitev konflikta obstoječih zahtev — POTRJENO

Faza 1 ima na `Invoice` eno polje:

```text
checksum
```

P0-20 neposredno zahteva kontrolni odtis za PDF, da je poslani PDF identičen arhiviranemu.

PRD arhivska sekcija pa govori o PDF in XML ter enem kontrolnem odtisu, ki naj bi “ustrezal obema datotekama”. Dve različni datoteki praviloma ne moreta imeti istega SHA-256.

Zato trenutnega besedila ni mogoče implementirati dobesedno brez dodatne razlage.

### 4.2 Potrjena rešitev

Za združljivost s Fazo 1:

```text
Invoice.checksum = SHA-256 finalnih PDF bytes
```

in Faza 2 doda:

```text
rendered_eslog_checksum
```

za SHA-256 finalnega XML.

V Fazi 4 `ArchivedFile` hrani checksum vsakega posameznega arhiviranega artifacta in preveri, da kopiji na lokalni ter iCloud lokaciji ustrezata istemu artifact checksumu.

**Ta odločitev je potrjena za FSD Faze 2.**

---

## 5. Datotečni izhod in varnost

### 5.1 Delovni render prostor — MUST

Faza 2 še ne implementira uradnega arhiva. Zato:

- `rendered_pdf_path` in `rendered_eslog_path` kažeta na generirani delovni artifact;
- Faza 4 kasneje kopira prav te bytes v uradni lokalni/iCloud arhiv;
- renderer sam ne odloča o lokalnem/iCloud/Dropbox arhivu.

Output root je konfiguriran v settings in ni sestavljen iz uporabniškega poljubnega absolute patha.

### 5.2 Ime finalne datoteke — MUST

Ker uradna arhivska struktura uporablja `{Stevilka}.pdf` / `{Stevilka}.xml`, je finalni basename:

```text
{varna_stevilka}.pdf
{varna_stevilka}.xml
```

Številka mora skozi strogo allow-list sanitizacijo za filename, čeprav jo generira aplikacija.

Podjetje/leto/mesec kot arhivska mapa sodijo v Fazo 4.

### 5.3 Ime vzorca — izvedbena odločitev

Predlog:

```text
VZOREC-{slugify_naziv(company.naziv)}.pdf
```

Ime vzorca nima poslovnega pomena in ni del uradnega arhiva.

### 5.4 Path traversal — MUST

Renderer:

- ne sprejema absolutnega output patha od uporabnika;
- basename ne sme vsebovati `/`, `\`, `..`, NUL ali nadomestnih separatorjev;
- končni resolved path mora ostati znotraj konfiguriranega render root-a;
- zapisuje prek začasne datoteke v isti dovoljeni mapi in atomskega replace/rename.

### 5.5 WeasyPrint lokalni viri — MUST

Produkcijska PDF predloga ne sme nalagati poljubnih `http://` ali `https://` URL-jev.

Dovoljeni so samo:

- statični asseti aplikacije;
- odobreni lokalni logo/signature asseti;
- `data:` URI samo, če ga renderer ustvari sam iz že validiranih bytes.

Uporabi se omejen `url_fetcher` oziroma enakovredna kontrola, ki zavrne oddaljene vire in lokalne poti zunaj allow-list rootov.

---

## 6. PDF renderer

### 6.1 API — MUST

Predlagani javni storitvi:

```python
render_pdf(invoice) -> RenderResult
render_sample_pdf(lease, *, datum_izdaje, obdobje_od, obdobje_do) -> SampleRenderResult
```

Točna imena so izvedbena odločitev; semantika je obvezna.

### 6.2 PDF render context — MUST

Template ne prejme ORM objektov neposredno.

Renderer najprej zgradi immutable DTO/context, ki vsebuje samo prikazne podatke. S tem template ne more po naključju slediti živim FK-jem.

Za finalni račun context nastane samo iz snapshotov.

### 6.3 Obvezna glava računa — MUST

PDF prikaže:

- tip dokumenta;
- številko;
- datum izdaje;
- obdobje storitve;
- rok plačila pri NP in ST;
- izdajatelj: naziv, naslov, davčna, matična, IBAN;
- prejemnik: naziv, naslov in identifikatorje, kadar so na snapshotu;
- oznako pogodbe pri dokumentih, ki jo imajo;
- naziv objekta, kadar obstaja.

Pri NP mora biti sklic na pogodbo jasno viden v glavi.

Pri ST je pogodba lahko prazna.

**Rok plačila pri PDF — potrjena poslovna odločitev:** Pri `NP` in `ST` je `rok_placila` za finalni PDF obvezen; PDF preflight zavrne manjkajočo vrednost. `STORNO` in `DOBROPIS` ne dobita novega roka plačila in ne kopirata roka izvirnika kot svojega roka. Njuno polje `rok_placila` je lahko `NULL`; PDF preflight ju zaradi tega ne zavrne. Renderer roka za korekcijo ne izračuna in ne kopira. PDF pri obeh jasno prikaže tip dokumenta, referenco na izvirni račun in razlog korekcije. BT-9 ima v uradnem eSLOG 2.0 modelu kardinalnost `0..1`, `G_SG8` pa je v XSD neobvezen. Korekcije `381` brez potrjenega roka plačila zato izpustijo `G_SG8`/BT-9; rok se ne kopira in ne izmišlja.

### 6.4 Logotip in podpis — MUST

PDF uporablja:

- snapshot logotipa po odločitvi §2.3.1;
- snapshot podpisnika iz `Invoice`;
- snapshot poti slike podpisa iz `Invoice`.

Manjkajoč obvezen asset pri finalnem računu je render napaka; renderer ne preklopi na trenutno `Company`/`Signatory` vrednost.

Pri vzorcu se uporabljajo trenutno preverjeni podatki podjetja/podpisnika.

### 6.5 Tabela postavk — MUST

Vsaka `InvoiceLine` se prikaže iz shranjenih vrednosti.

Najmanj:

- opis;
- osnova;
- DDV stopnja oziroma ustrezna oznaka pri 0 % / posebnem režimu;
- DDV znesek;
- skupaj.

Opis se ne sestavlja ponovno iz `Lease`/`Unit`/`Property`; uporabi se shranjeni `InvoiceLine.opis`.

Zneski so desno poravnani in prikazani na dve decimalki.

### 6.6 Številčni in datumski prikaz — MUST

PDF je v slovenski lokalizaciji:

- decimalna vejica;
- dve decimalni mesti za denarne zneske;
- tisočice vizualno ločene;
- datum v človeku razumljivem slovenskem formatu;
- valuta je EUR in ni dinamična.

Renderer hrani vrednosti kot `Decimal`; lokalizacija je samo prikaz.

### 6.7 DDV rekapitulacija — MUST

Rekapitulacija nastane iz snapshotov `InvoiceLine`.

Pravila:

- `OBDAVCENO`: skupina po `ddv_rekap_skupina` in stopnji, prikaz osnove + DDV;
- `OPROSCENO`: ločena skupina, DDV 0, prikaže se snapshot klavzula;
- `OBRNJENA_DAVCNA`: ločena skupina, DDV 0, prikaže se snapshot klavzula;
- `NI_PREDMET`: ni del DDV rekapitulacije, ostane pa v skupnem znesku dokumenta.

Renderer ne bere trenutnega `VatRegime`.

Vsota prikazne rekapitulacije se mora ujemati s snapshot total-i. Neskladje je napaka.

### 6.8 Klavzule — MUST

Vsaka zahtevana DDV klavzula se izpiše iz `InvoiceLine.ddv_klavzula`.

Enaka klavzula se na dokumentu lahko deduplicira za prikaz, ne sme pa se vsebinsko prepisovati ali normalizirati.

### 6.9 Totali — MUST

PDF jasno prikaže:

- `osnova_skupaj`;
- `ddv_skupaj`;
- `skupaj_z_ddv`.

Renderer pred izpisom preveri:

```text
sum(InvoiceLine.osnova) == Invoice.osnova_skupaj
sum(InvoiceLine.ddv_znesek) == Invoice.ddv_skupaj
sum(InvoiceLine.skupaj) == Invoice.skupaj_z_ddv
```

z natančnostjo shranjenih `Decimal(10,2)` vrednosti.

### 6.10 STORNO — MUST

PDF mora jasno pokazati, da gre za STORNO, in referenco na izvirni dokument.

Vrstice uporablja take, kot so shranjene na storno `InvoiceLine`; renderer jih ne negira ponovno.

Prikaže se `razlog`.

### 6.11 DOBROPIS — MUST

PDF mora jasno pokazati DOBROPIS in referenco na izvirni dokument.

Uporabi ročno vnesene snapshot vrstice dobropisa. Renderer zneskov ne računa iz pogodbe.

Prikaže se `razlog`.

### 6.12 Večstranski PDF — MUST kot izvedbena zahteva

Predloga mora varno podpreti poljubno število vrstic.

Minimalna pravila:

- glava tabele se ponovi na novi strani;
- posamezne vrstice naj se, kjer je mogoče, ne lomijo med stranmi;
- totals + DDV rekapitulacija naj ostaneta skupaj, kadar prostor dopušča;
- footer ne prekriva vsebine;
- številka strani je dovoljena in priporočena.

To je izvedbena zahteva za robustnost, ne novo poslovno pravilo.

### 6.13 Determinističnost — MUST

Za iste snapshot podatke, isto verzijo template-a, iste lokalne assete in isto verzijo rendererja mora PDF vsebinsko ostati isti.

Ker knjižnice lahko v PDF zapišejo metapodatke, ki vplivajo na bytes, mora implementacija izbrati eno od dveh pravil:

1. zagotoviti byte-deterministic PDF; ali
2. enkrat ustvarjene finalne bytes obravnavati kot kanonični artifact in jih vedno ponovno uporabljati.

Za P0-20/P0-25 je obvezno vsaj pravilo 2.

---

## 7. Vzorčni PDF

### 7.1 Namen — MUST

Vzorec je preverjanje dejanskega videza produkcijske PDF-predloge pred prvo produkcijsko izdajo.

### 7.2 Vir podatkov — MUST

Vzorec ne uporablja `Invoice`, ker ne sme ustvariti izdanega ali osnutnega davčnega dokumenta.

Rendering storitev sme uporabiti obstoječe domenske storitve Faze 1 za:

- preflight pogodbe;
- izračun kandidata;
- izbiro veljavnega podpisnika;
- DDV izračun.

Renderer sam teh pravil ne duplicira.

### 7.3 Varnostna meja — MUST

`render_sample_pdf(...)`:

- ne ustvari `Invoice`;
- ne ustvari `InvoiceLine`;
- ne kliče NumberSequence;
- ne spremeni `NumberSequence`;
- ne ustvari delivery zapisa;
- ne ustvari uradnega archive zapisa;
- ne spreminja podatkov pogodbe;
- sme zapisati samo začasni/vzorčni PDF artifact.

### 7.4 Vizualna oznaka — MUST

Na vsaki strani je jasno vidna oznaka:

```text
VZOREC – NI RAČUN
```

Vzorec nima uradne številke.

### 7.5 Ista produkcijska predloga — MUST

Vzorec uporablja isto osnovno HTML/CSS predlogo in iste komponente kot finalni PDF.

Dovoljena razlika je samo sample mode:

- watermark/oznaka vzorca;
- odsotnost uradne številke;
- vzorčni render metapodatki.

Ločena “sample-only” predloga, ki bi lahko vizualno odstopala od produkcije, ni dovoljena.

### 7.6 Potrditev vzorca — OUT OF SCOPE Phase 2

Faza 2 zagotovi:

- verzijo predloge;
- vzorčni PDF;
- informacije, ki jih poznejši čarovnik potrebuje za potrditev.

Shranjevanje uporabnikove potrditve, čas potrditve, stale/valid status in UI čarovnika sodijo v prihodnji FSD prvega zagona / UI fazo.

---

## 8. eSLOG 2.0

### 8.1 Stanje specifikacije — 2B-0 ZAKLJUČEN

PRD zahteva eSLOG 2.0 in XSD validacijo. SAD izrecno prestavi **natančno eSLOG shemo in preslikavo polj** v FSD Faze 2.

Uradni paket eSLOG 2.0, XSD `v200`, namespace definicije, navodila in primer so pridobljeni ter dokumentirani v `rendering/xsd/eslog_2_0/README.md`. O5 in 2B-0 sta zaključena.

Odprta preslikava fizične osebe brez davčne številke ni blocker za 2B-1. Ostane eksplicitna mapping naloga za 2B-2; identifikatorja ali placeholderja ni dovoljeno izumiti.

### 8.2 Pridobljeni artefakti — ZAKLJUČENO v 2B-0

V repo so dodani oziroma dokumentirano referencirani:

1. uradni XSD paket za točno uporabljeno verzijo eSLOG 2.0;
2. uradna navodila za preslikavo;
3. davčne kategorije/kodne liste;
4. pravila referenc na izvirni račun za storno/dobropis;
5. pravila pogodbenih referenc;
6. najmanj en uradno veljaven primer XML za regresijski test;
7. uradno definicijo obveznih/neobveznih segmentov ter dovoljenih optional ključev in tipov, potrebnih za konfiguracijo iz PRD §10.3.

Izvor in verzija artefaktov sta dokumentirana v `rendering/xsd/eslog_2_0/README.md`.

### 8.3 Konceptualna preslikava — MUST

Ko so uradni artefakti pridobljeni, se FSD FINAL dopolni s tabelo:

| Domenski podatek | Vir | eSLOG cilj |
|---|---|---|
| številka računa | `Invoice.stevilka` | TBD po uradni shemi |
| tip dokumenta | `Invoice.document_type` | TBD |
| datum izdaje | `Invoice.datum_izdaje` | TBD |
| obdobje storitve | `Invoice.obdobje_od/do` | TBD |
| rok plačila | `Invoice.rok_placila` | TBD |
| izdajatelj | snapshot `Invoice.izdajatelj_*` | TBD |
| prejemnik | snapshot `Invoice.prejemnik_*` | TBD |
| IBAN | `Invoice.izdajatelj_trr` | TBD |
| pogodba | `Invoice.oznaka_pogodbe` | BT-12; `G_SG1/S_RFF/C_C506`, kvalifikator `CT`, vrednost v `D_1154` |
| opis postavke | `InvoiceLine.opis` | TBD |
| osnova | `InvoiceLine.osnova` | TBD |
| DDV stopnja | `InvoiceLine.ddv_stopnja` | TBD |
| DDV znesek | `InvoiceLine.ddv_znesek` | TBD |
| DDV kategorija | `InvoiceLine.ddv_eslog_koda` po potrditvi modela | TBD |
| klavzula | `InvoiceLine.ddv_klavzula` | BT-120; `S_FTX[D_4451='AGM']/C_C108/D_4440` |
| totals | `Invoice.osnova_skupaj/ddv_skupaj/skupaj_z_ddv` | TBD |
| popravek originala | `Invoice.povezan_racun.stevilka` | BT-25; `G_SG1/S_RFF/C_C506`, kvalifikator `OI`, vrednost v `D_1154`; datum izvirnika BT-26, kvalifikator `384` |

Za `DOBROPIS` se uporabi uradna koda dokumenta `381`.

Za interni tip `STORNO` se v eSLOG uporabi koda `381` ter ista referenca BT-25/BT-26 na izvirni račun. To je **projektna preslikava**, ne uradna eSLOG koda z nazivom »STORNO«.

Beseda `TBD` je namerna in se pred implementacijo zamenja z dejanskimi uradnimi elementi.

### 8.3.1 Potrjene projektne preslikave 2B-2A

Partnerja uporabljata kvalifikatorja `SE` in `BY`. Država se zapiše iz snapshot polja v `S_NAD/D_3207`; enovrstični `naslov` ostane v `C_C059/D_3042`. Matična številka se zapiše iz snapshota z `G_SG3/S_RFF/C_C506/D_1153 = 0199`.

Identificiran partner se zapiše z `VA` in `AHP`; neidentificiran partner samo z `AHP`. Če račun vsebuje kategorijo `O`, se davčna identifikatorja partnerjev zapišeta samo z `AHP`. Renderer bere samo `Invoice` in `InvoiceLine` snapshote. Skupine za DDV niso podprte v v1.

Za v1 ena `InvoiceLine` predstavlja eno agregirano obračunano storitev: BT-129 količina je `1`, BT-130 enota je `C62`, BT-146 neto cena enote pa je `InvoiceLine.osnova`. Dodatna modelna polja za količino, enoto ali ceno se ne dodajo.

Kategorija `O` se ne sme združiti z obdavčenimi postavkami kategorije `S` na istem eSLOG računu. Potrditev to preveri pred dodelitvijo številke. Pravilo se ne razširi na kombinacije `O` z `E` ali `AE`. Vir: EPOS, pojasnilo z dne 24. 6. 2022: https://www.epos.si/novice/dopolnitev-pojasnila-o-uporabi-dav%C4%8Dnih-kod-v-e-ra%C4%8Dunih-e-slog-2-0
### 8.4 XSD validacija — MUST

XML ni uspešno renderiran, dokler:

1. ni sintaktično veljaven XML;
2. ne prestane uradne XSD validacije;
3. številka in vsi zneski niso enaki snapshot podatkom, ki jih uporablja PDF;
4. finalni bytes niso shranjeni.

Napaka validacije:

- ne spremeni računa;
- ne odstrani številke;
- ne ustvari “veljavnega” `rendered_eslog_path`;
- vrne uporabno diagnostiko za razvoj/log.

### 8.5 XML varnost — MUST

Pri `lxml`:

- brez omrežnega dostopa;
- brez external entity resolution;
- brez nalaganja poljubnih DTD;
- XSD se bere samo iz verzioniranega lokalnega allow-list direktorija.

### 8.6 Neobvezni eSLOG segmenti in polja — MUST po pridobitvi uradne sheme

FINAL PRD določa:

- obvezni eSLOG segmenti izhajajo iz XSD in niso uporabniško izklopljivi;
- neobvezni segmenti so nastavljivi;
- privzeta nastavitev je na nivoju podjetja;
- prejemnik/najemnik jo lahko povozi;
- neobvezna polja se modelirajo kot preslikava `ključ → vrednost`, ne kot množica fiksnih stolpcev.

Faza 2 zato ločeno modelira stanje neobveznih segmentov in njihove vrednosti:

```text
Company privzeto stanje neobveznih segmentov
        ↓ povozijo samo izrecni Tenant override-i
učinkovito stanje neobveznih segmentov

Tenant neobvezne vrednosti polj (ključ → vrednost)
```

Company ne hrani privzetih vrednosti neobveznih polj. Tretji implicitni oziroma sistemski sloj privzetih vrednosti ne obstaja.

Konfiguracijska verzija `eslog-2.0-v200-config-v1` podpira samo:

| Vrsta | Interni ključ | Uradni termin | Preslikava |
|---|---|---|---|
| segment | `elektronski_naslov_kupca_vkljucen` | BT-49/BT-49-1 | kupec `G_SG2[BY]`, `G_SG5`, CTA `IC`, COM `EM` |
| segment | `kontaktni_podatki_kupca_vkljuceni` | BG-9 | kupec `G_SG2[BY]`, `G_SG5`, CTA `PD` |
| Tenant polje | `elektronski_naslov_kupca` | BT-49 | `D_3148`, največ 512 znakov, veljaven e-naslov; shema `EM` |
| Tenant polje | `kontaktna_tocka_kupca` | BT-56 | `D_3412`, največ 256 znakov |
| Tenant polje | `kontaktni_telefon_kupca` | BT-57 | `D_3148`, največ 512 znakov; kvalifikator `TE` |
| Tenant polje | `kontaktni_email_kupca` | BT-58 | `D_3148`, največ 512 znakov, veljaven e-naslov; kvalifikator `EM` |

Oba Company segmentna defaulta sta izrecno `False`; s tem konfiguracija v1 ohrani današnji XML. `Tenant.email` ni nadomestni vir za BT-49. BT-10 in BT-19 nista podprta v v1, ker ni odločeno, ali sta last Tenant, Lease ali posameznega Invoice.

Pravila:

1. Obveznega XSD segmenta ni mogoče izklopiti.
2. Override prejemnika spremeni samo izrecno podane ključe; ostalo podeduje od podjetja.
3. Neznan ali nepodprt ključ se ne sme tiho zapisati v XML.
4. Vrednosti se validirajo glede na uradno eSLOG shemo/mapping pred generiranjem.
5. Renderer ne sme iz inventiranih ključev sklepati XML elementov.
6. Natančni dovoljeni ključi, tipi in preslikave se v FINAL FSD dopolnijo šele iz uradnega, verzioniranega eSLOG 2.0 artefakta.
7. Če konfiguracija povzroči XSD-neveljaven dokument, render eSLOG ni uspešen.
8. Omogočen BT-49 segment zahteva izrecno vrednost BT-49; izklopljen segment z vrednostjo je napaka.
9. Omogočen BG-9 zahteva najmanj enega izmed BT-56, BT-57 ali BT-58; izklopljen segment s katerokoli od teh vrednosti je napaka.
10. Vrednosti se ne ignorirajo in konfiguracija odpove zaprto.
11. Izdani snapshot vsebuje vse segmentne boolean vrednosti brez stanja `inherit` in že normalizirana polja.
12. Verzija snapshota v celoti zamrzne semantiko; stare verzije nikoli ne podedujejo novih ključev ali prihodnjih defaultov.

Zgodovinski baseline v1 je:

```yaml
version: eslog-2.0-v200-config-v1
segments:
  elektronski_naslov_kupca_vkljucen: false
  kontaktni_podatki_kupca_vkljuceni: false
fields: {}
```

Vir konfiguracije za `STORNO` in `DOBROPIS` ostaja `UNRESOLVED`: poznejša odločitev mora izbrati kopiranje konfiguracije izvirnika ali razrešitev trenutne Company/Tenant konfiguracije.

**Scope meja:** podatkovni/service model, potreben za pravilno generiranje XML, sodi v Fazo 2. Uporabniški UI za urejanje teh nastavitev ostane P1/poznejša faza, skladno s PRD.

### 8.7 Fizične osebe — POTRJENO, mapping brez davčne številke UNRESOLVED

Poslovna odločitev Faze 2:

- eSLOG se generira, validira in arhivira za vsak račun;
- davčna številka za `FIZICNA_OSEBA` ni predpogoj za izdajo;
- eSLOG kanal za fizično osebo ostaja prepovedan.

Odločitev temelji na snapshotu `Invoice.prejemnik_tip`, ne na živem `Tenant`.

Uradna dokumentacija potrjuje zapis fizične osebe brez identifikatorja za DDV in brez matične številke, kadar ima davčno številko NBT-013/AHP. Za fizično osebo brez davčne številke uradna preslikava ni določena. Ta primer ostane `UNRESOLVED` za 2B-2; ne blokira 2B-1 in ne dovoljuje izmišljenega identifikatorja ali placeholderja.

---

## 9. Statusi in napake

### 9.1 Renderer ne vodi delivery statusa — MUST

Faza 2 ne nastavlja:

- `POSLAN`;
- `NAPAKA` delivery pomena;
- `REZULTAT_NI_ZNAN`.

To je Faza 4.

### 9.2 Render napaka — MUST

Za Fazo 2 naj render storitev vrne strukturirano napako oziroma exception z vsaj:

- stabilno kodo;
- človeku razumljivim opisom;
- artifact tipom (`PDF`/`ESLOG`);
- po potrebi podrobnostjo validacije.

Ni nujno dodati novega `Invoice.status` samo zaradi render napake.

### 9.3 Poslovni snapshot ostane nedotaknjen — MUST

Ob katerikoli render napaki:

- številka ostane ista;
- totals ostanejo isti;
- vrstice ostanejo iste;
- status izdaje se ne vrne v osnutek;
- neuspel artifact ne dobi finalne poti/checksuma.

---

## 10. PDF template versioning

### 10.1 Verzija — MUST

Produkcijska predloga ima eksplicitno verzijo, npr.:

```text
invoice-v1
```

Sprememba poslovno pomembne vsebine ali strukture pomeni novo verzijo.

Stare verzije, uporabljene na izdanih računih, se ne prepisujejo.

### 10.2 Spremembe predloge

**Računovodsko pomembna sprememba** najmanj:

- dodajanje/odstranitev obveznega podatka;
- sprememba prikaza DDV;
- sprememba totals;
- sprememba pogodbenega sklica;
- sprememba logike prikaza storna/dobropisa.

Takšna sprememba mora pozneje razveljaviti uporabnikovo potrditev vzorca.

**Manjša vizualna sprememba**:

- spacing;
- nenevarna tipografija;
- kozmetična poravnava brez vpliva na vsebino.

Natančna avtomatika zastarelosti potrditve ostaja zunaj Faze 2, vendar renderer mora izpostaviti version identifier.

---

## 11. Testni načrt Faze 2

### 11.1 Skupni testi — MUST

- finalni render zavrne neoštevilčen Invoice;
- sintetični migracijski Invoice se ne renderira kot uradni račun;
- renderer ne spremeni poslovnih snapshot polj;
- renderer ne spremeni NumberSequence;
- neuspeh ne pusti delne finalne datoteke;
- unsafe filename/path je zavrnjen;
- remote asset URL je zavrnjen;
- ponovni klic na veljaven že renderiran artifact vrne isti artifact;
- spremenjen checksum/missing file sproži integrity error.

### 11.2 PDF — MUST

1. osnovni NP z eno vrstico;
2. več vrstic;
3. mešan `OBDAVCENO + OPROSCENO`;
4. `NI_PREDMET`;
5. `OBRNJENA_DAVCNA`;
6. klavzula se izpiše iz snapshota;
7. več rekapitulacijskih skupin;
8. totals se ujemajo;
9. mismatch vrstic in Invoice totals blokira render;
10. slovenski prikaz zneskov;
11. pogodbeni sklic + objekt;
12. ST brez pogodbe;
13. logotip;
14. podpis + funkcija;
15. brez neobvezne funkcije podpisnika;
16. večstranski račun;
17. STORNO + referenca + razlog;
18. DOBROPIS + referenca + razlog;
19. sprememba `Tenant` po izdaji ne spremeni PDF;
20. sprememba `Company` po izdaji ne spremeni snapshot vsebine;
21. sprememba `VatRegime` po izdaji ne spremeni PDF;
22. sprememba `Lease`/`LeaseItem` po izdaji ne spremeni PDF;
23. varen filename/path;
24. drugi render ne ustvari drugega artifacta;
25. checksum je SHA-256 dejanskih PDF bytes po sprejeti odločitvi §4;
26. zamenjava trenutnega `Company.logotip` po izdaji ne spremeni izdanega PDF;
27. zamenjava trenutnega podpisa po izdaji ne spremeni izdanega PDF;
28. stari logo/podpis asset, ki ga referencira izdani račun, ostane dosegljiv in ga običajna zamenjava ne prepiše.

### 11.3 Vzorčni PDF — MUST

- ne ustvari `Invoice`;
- ne ustvari `InvoiceLine`;
- ne spremeni `NumberSequence`;
- ne dodeli številke;
- ne ustvari delivery/archive zapisa;
- uporablja isto production template verzijo;
- vsebuje `VZOREC – NI RAČUN`;
- vsebuje logo, podpis, podjetje, najemnika, vrstice, DDV, rok in IBAN;
- test za dve LeaseItem na isti Lease;
- test za mešan DDV.

### 11.4 eSLOG — MUST v 2B-2

- minimalni veljavni XML;
- pravna oseba kot prejemnik;
- več vrstic;
- mešani DDV režimi;
- oproščena postavka;
- pogodbeni sklic;
- totals;
- STORNO referenca;
- DOBROPIS referenca;
- XSD success;
- XSD failure;
- manjkajoča obvezna `eslog_koda`;
- sprememba živega `VatRegime` po izdaji ne spremeni XML;
- XML brez external entity/network resolution;
- varen output path;
- idempotenten ponovni klic;
- checksum XML po §4;
- fizična oseba: XML se generira, validira in arhivira; dostavni kanal `ESLOG` ostane prepovedan;
- fizična oseba brez davčne številke: mapping ostane eksplicitno `UNRESOLVED`, brez izmišljenega identifikatorja ali placeholderja;
- obveznega XSD segmenta ni mogoče izklopiti prek konfiguracije;
- Company default neobveznega segmenta se uporabi, če ni override-a;
- Tenant/recipient override pravilno povozi samo izrecno podane ključe;
- neznan optional ključ je zavrnjen in se ne pretvori v poljuben XML element;
- konfiguracija, ki povzroči XSD-neveljaven XML, je zavrnjena.
- oba Company defaulta v1 sta izrecno `False`;
- `None` ni stanje dedovanja; manjkajoč Tenant ključ pomeni dedovanje Company vrednosti;
- vključen BT-49 segment zahteva veljaven izrecni BT-49, izključen pa njegovo prisotnost zavrne;
- vključen BG-9 zahteva najmanj BT-56, BT-57 ali BT-58, izključen pa njihovo prisotnost zavrne;
- BT-49 ne uporablja `Tenant.email` kot nadomestnega vira;
- BT-10 in BT-19 sta v v1 zavrnjena;
- snapshot v1 vsebuje obe razrešeni boolean stanji in normalizirana polja;
- stari snapshot ne podeduje novih ključev ali prihodnjih defaultov.

### 11.5 Integracijski test — MUST

Pot:

```text
Company + Signatory
→ Property + Unit + Tenant + Lease
→ dve LeaseItem z različnim DDV
→ priprava NP
→ potrditev + številka
→ PDF render
→ eSLOG render (ko je 2b odblokiran)
```

Preveri:

- isti snapshot podatki;
- isti totals do centa;
- številka enaka;
- nič ponovnega računanja iz spremenjene pogodbe;
- path/checksum metapodatki nastavljeni samo po uspehu.

---

## 12. Kaj Faza 2 NE implementira

### OUT OF SCOPE

- Django UI za mesečni cikel;
- čarovnik prvega zagona kot UI;
- model in workflow uporabnikove potrditve vzorca;
- pošiljanje PDF/XML;
- SMTP/Keychain;
- `DeliveryAttempt`;
- statusi rezultata pošiljanja;
- uradno arhiviranje lokalno + iCloud;
- Dropbox backup;
- `ArchivedFile` / `BackupSnapshot`;
- accounting export;
- kontrolni dokument;
- dejanska e-pot / UJP integracija;
- UI konfiguracija neobveznih eSLOG segmentov;
- dashboard/search/filter.

Renderer pa mora imeti API, ki ga te poznejše faze lahko uporabijo brez spremembe poslovnih pravil.

---

## 13. Predlagan vrstni red implementacije

### 2A-1 — osnova `rendering`

- Django app/modul;
- render context DTO;
- preflight;
- varen output path;
- render result/error tipi;
- testi brez WeasyPrint vizualne kompleksnosti.

### 2A-2 — PDF template

- WeasyPrint;
- glava;
- postavke;
- totals;
- DDV rekapitulacija;
- logo/podpis;
- STORNO/DOBROPIS.

### 2A-3 — vzorčni način

- candidate → sample context;
- ista template;
- watermark;
- noben DB side effect izdaje.

### 2A-4 — robustnost

- multi-page;
- idempotency;
- checksum;
- asset security;
- template version.

### 2B-0 — pridobitev uradnega eSLOG paketa

**ZAKLJUČENO.** Uradni paket, XSD in provenance/mapping README so verzionirani v `rendering/xsd/eslog_2_0/`.

### 2B-1 — model snapshot dopolnitve

Po uradni shemi potrdi, katera polja/kode manjkajo.

### 2B-2 — eSLOG mapping + XSD

- lxml;
- lokalna XSD;
- mapping tabela;
- validacija;
- varnost;
- testi z uradnimi primeri.
- razrešitev preslikave fizične osebe brez davčne številke brez izmišljenega identifikatorja ali placeholderja.

### 2B-3 — Phase 2 integration review

Celoten `pytest`, Django check, migrations check, Ruff in pregled proti temu FINAL FSD.

---

## 14. Status odločitev pred FINAL

| # | Vprašanje | Odločitev | Status |
|---|---|---|---|
| O1 | Snapshot logotipa | `Invoice.izdajatelj_logotip_slika_pot` | **ZAPRTO** |
| O2 | Verzija PDF predloge | `Invoice.pdf_template_version`, določen ob potrditvi/izdaji | **ZAPRTO** |
| O3 | Checksum PDF + XML | `Invoice.checksum` = PDF SHA-256; `rendered_eslog_checksum` = XML SHA-256 | **ZAPRTO** |
| O4 | eSLOG koda | `InvoiceLine.ddv_eslog_koda` kot snapshot | **ZAPRTO** |
| O5 | Uradni eSLOG XSD/mapping | uradni paket `v200` pridobljen, verzioniran in dokumentiran | **ZAPRTO — 2B-0 zaključeno** |
| O6 | eSLOG za fizične osebe | generira, validira in arhivira se za vsak račun; dostava `ESLOG` ostaja prepovedana; mapping brez davčne številke ostaja odprt za 2B-2 | **ZAPRTO kot poslovna odločitev** |
| O7 | Recovery manjkajočega PDF | ločena recovery storitev; nadomestitev dovoljena samo ob istem checksumu | **ZAPRTO** |

### 14.1 Potrebni minimalni popravki višjih zahtev pred FINAL

Zaradi potrjene odločitve O3 je potrebna ena majhna uskladitev PRD v1.2: **PRD §11.2** naj namesto enega SHA-256 za PDF in XML določi ločen kontrolni odtis na posamezen artifact; obstoječi `Invoice.checksum` ostane PDF checksum, XML dobi `rendered_eslog_checksum`.

---

## 15. Kriterij zaključka Faze 2

### PDF 2a je READY, ko:

- vse MUST zahteve PDF dela so implementirane;
- vzorčni način nima side effectov izdaje;
- vseh pet podjetij je tehnično mogoče renderirati v sample načinu;
- snapshot isolation je dokazano s testi;
- checksum/idempotency pravilo je potrjeno in testirano;
- full suite ostane zelen.

### eSLOG 2b je READY, ko:

- uradni eSLOG artefakti so verzionirani in dokumentirani;
- mapping tabela nima `TBD` pri obveznih elementih;
- XML se generira iz snapshotov;
- XSD validacija je obvezna;
- totals/številka se ujemajo s PDF podatki do centa;
- eSLOG security testi so zeleni.

### Faza 2 je zaključena, ko:

- 2a in 2b sta READY;
- ni odprtega blockerja v §14;
- renderer ne uvaja UI/delivery/archive poslovne logike;
- integracijski test Faza 1 → rendering je zelen.

---

## 16. Trenutna ocena pripravljenosti DRAFT-a

**PDF 2a:** specifikacijsko pripravljen po minimalni uskladitvi PRD §11.2; implementacija lahko začne z 2A, če se ta popravek zapiše.  
**eSLOG 2b:** poslovne odločitve so zaprte; produkcijska implementacija ostaja blokirana samo zaradi manjkajočega uradnega XSD/mapping paketa.  
**Koda:** ta DRAFT sam ne implementira sprememb. Faza 2 bo zahtevala migracijo za nova snapshot/render polja ter dopolnitev issuance storitev.  
**Naslednji korak:** minimalno uskladiti PRD §10.4 in §11.2, pridobiti uradni eSLOG paket, nato dokument označiti FINAL in implementirati najprej 2A.
