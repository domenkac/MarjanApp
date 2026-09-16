# PRD — Aplikacija za izdajanje najemniških računov

**Različica:** 1.2 · **Datum:** 16. september 2026 · **Status:** za potrditev naročnika

**Nadomešča:** PRD v1.1. Različica 1.2 nadomešča v1.1; v1.1 ostaja nespremenjena kot prejšnja potrjena različica.

**Spremembe v1.2:** dodan je čarovnik prvega zagona s celotno verigo podatkov za pripravo računov; uvedena sta uporabnikova potrditev vzorca za vseh pet podjetij in ločena uporabnikova potrditev DDV-nastavitev; določena so pravila vzorčnega PDF-ja in zastarelosti potrditev; uspešen neuničujoč preizkus lokalnega arhiva, iCloud arhiva in Dropbox mape je določen kot pogoj zaključka začetne nastavitve; odstranjene so vse potrjevalne vloge računovodje, njegovi vlogi prejemnika BCC in kontrolnega dokumenta pa ostajata nespremenjeni; določena so tudi pravila fizičnega izbrisa, arhiviranja, ponovne aktivacije in minimalne revizijske sledi.

**Spremembe v1.1:** uskladitev s FSD faze 1 v1.1 — pojasnjen prehod iz Excela, indeksacija od februarja in morebitni poračun, primerjava z zadnjim dejansko izdanim računom, posamična atomarnost potrditve ter zaprto vprašanje o nazivu podjetja v imenu datoteke. Dodani so zaključna kontrola pošiljanja, zbirno obvestilo uporabniku, jasna vizualna potrditev uspešnih in neuspešnih korakov ter varna obravnava primerov, ko rezultat pošiljanja ni znan.

| | |
|---|---|
| **Namen dokumenta** | Opredelitev obsega, funkcij in poslovnih pravil pred začetkom razvoja |
| **Ciljna publika** | Naročnik (potrditev obsega), razvijalec (skupaj s SAD in FSD kot izvedbeno izhodišče) |
| **Nadaljnji dokumenti** | SAD v1.2, FSD faze 1 v1.2 (osnutek) in UI/UX prototipi — **dostavljeni**; FSD prvega zagona in Testni načrt — v pripravi |
| **Kaj ta dokument ni** | Ni tehnična specifikacija in ne opisuje implementacije |

---

## 1. Poslovni problem

Naročnik je nepremičninska družba, ki prek petih podjetij oddaja lastne poslovne prostore in objekte. Vsak mesec izda približno 15 računov za najemnino. Postopek danes teče v Excelu: ročno prepisovanje podatkov, ročno številčenje, ročna izdelava PDF-jev, ročno pošiljanje.

**Stroški obstoječega stanja:**

- **Čas** — nekaj ur mesečno za opravilo, ki je v celoti ponovljivo.
- **Tveganje napak** — napačen znesek, podvojena ali preskočena številka, izpuščen račun, napačen prejemnik.
- **Odsotnost sledljivosti** — ni zapisa, kdo je kaj spremenil in kdaj.
- **Ročna kontrola davčnih pravil** — mešani DDV režimi (oproščeno / obdavčeno) se obvladujejo po spominu.

Obseg je majhen, postopek pa ponavljajoč, formaliziran in davčno občutljiv. To je natanko profil, ki ga preglednica obvladuje slabo.

---

## 2. Cilji

| # | Cilj | Merilo |
|---|---|---|
| C1 | Skrajšanje mesečnega ciklusa | Pod 10 minut od zagona do zadnjega poslanega računa |
| C2 | Odprava ročnega vnosa zneskov | Pri rednem mesecu ni ročno vnesenega zneska; vsi izpeljani iz pogodb |
| C3 | Odprava napak v številčenju | Nič podvojenih, nič tiho preskočenih številk |
| C4 | Popolna sledljivost | Za vsak dokument je razviden nastanek, spremembe, pošiljanje in arhiviranje |
| C5 | Podatkovna suverenost | Vsi podatki lokalno, brez SaaS odvisnosti, izvozljivi |
| C6 | Pripravljenost na e-račun | eSLOG 2.0 se generira in arhivira od prvega dne |
| C7 | Zanesljiva kontrola za računovodjo | Mesečni kontrolni dokument omogoča neodvisno preverjanje |

**Končni kriterij sprejema:** uporabnik zažene mesečni postopek, pregleda en zaslon s primerjavo glede na zadnji dejansko izdani račun, potrdi, in vsi računi so izdani, poslani, arhivirani in evidentirani — brez odpiranja posameznih PDF-jev.

---

## 3. Ne-cilji

| Ne-cilj | Razlog |
|---|---|
| Knjigovodstvo, glavna knjiga, obračun DDV | To opravlja računovodski servis |
| Spremljanje plačil, terjatve, opomini, zamudne obresti | Drugačen podatkovni vir (bančni izpiski); kandidat za v2 |
| Mobilna različica, več uporabnikov, sinhronizacija naprav | Zahteva strežnik in spremeni arhitekturo |
| Upravljanje pogodb kot dokumentov (podpisovanje, opomniki rokov) | Aplikacija hrani parametre pogodbe, ne pogodbenega procesa |
| Oddaja e-računov prek ponudnika e-poti | v1 pripravi eSLOG XML; dejanska pot je v2 |
| Obračun obratovalnih stroškov | Najemnik jih plačuje neposredno upravniku oz. dobavitelju |
| Več valut | Vse pogodbe so v EUR; valuta se ne modelira kot spremenljivka |

### 3.1 Meja posplošitve

Naročnik je zavrnil obstoječe rešitve (Minimax/Saop, eRačuni.com) zaradi prevelike kompleksnosti. Aplikacija mora ostati namenska. Hkrati mora prenesti spremembe zakonodaje in morebitno novo dejavnost. Ločnica:

| Posploši | Ne posploši |
|---|---|
| **Podatke** — šifranti DDV režimov, storitev, enot mere | **Delovni tok** — mesečni ciklus ostane najemniški |
| **Parametre izračuna** — stopnja, klavzula, enota | **Načine izračuna** — zaprt nabor (§6.2) |
| Nazive, besedila, predloge | Zaslone in korake |

**Pravilo odločanja:** če nova zahteva pomeni novo vrstico v tabeli, se posploši. Če pomeni nov zaslon ali novo vejo v izračunu, se počaka, da nastopi.

Novo dejavnost bo naročnik zaračunaval prek ročne storitvene fakture (§8) z novim vnosom v šifrant storitev. To ne zahteva dodatnega razvoja.

---

## 4. Uporabniki

| Vloga | Opis | Dostop |
|---|---|---|
| **Izdajatelj računov** | En uporabnik, en MacBook. Pripravlja, pregleduje in pošilja račune. | Polni |
| **Računovodja** | Lahko prejema kopije računov (BCC) in mesečni kontrolni dokument. Dokumentov, DDV-nastavitev ali pravil ne potrjuje in ne vpliva na izdajo ali statuse postopkov. | Brez dostopa do aplikacije |
| **Najemnik** | Prejema račun po e-pošti (PDF in/ali eSLOG). | Brez dostopa |

Aplikacija v v1 nima prijave, vlog ali pravic. Zaščita je na nivoju naprave (opcijsko geslo ali Touch ID).

---

## 5. Pregled rešitve

```
ŠIFRANTI                POGODBE                MESEČNI CIKLUS
─────────               ────────               ──────────────
DDV režimi         →    Najemna pogodba   →    1. Izbira meseca
Storitve                ├─ Najemnik            2. Predhodna kontrola
Enote mere              ├─ Enota               3. Izračun osnutkov
                        ├─ Postavke            4. Paketni pregled (Δ)
PODJETJA                ├─ Veljavnost          5. Obravnava izjem
────────                ├─ Indeksacija         6. Potrditev + številke
5 izdajateljev     →    └─ Plačilni pogoji     7. Pošiljanje
├─ Številčenje                                 8. Arhiviranje
├─ Podpisnik                                        │
├─ E-pošta                   ROČNE ST FAKTURE       ▼
└─ Računovodja               (kadarkoli)       KONTROLNI DOKUMENT
                                                (za računovodjo)
```

---

## 6. Podatkovni model in poslovna pravila

### 6.1 Jedro

Osrednja entiteta je **najemna pogodba**, ne stranka s predlogo. Znesek računa je iz pogodbe **izračunljiv**, ne prepisan — to je pogoj za avtomatizacijo pregleda (§7).

```
Podjetje (izdajatelj)                    5 zapisov
  └── Nepremičnina
        └── Enota (prostor, pisarna, skladišče, parkirno mesto)
              └── NAJEMNA POGODBA
                    ├── Oznaka pogodbe (št. iz podpisane pogodbe)
                    ├── Najemnik (pravna ali fizična oseba)
                    ├── Kontakti (TO / CC / BCC, po kanalu)
                    ├── Postavke (vsaka s svojim DDV režimom)
                    ├── Trajanje (§6.10): tip, začetek, konec,
                    │     samodejno podaljšanje, odpoved, status
                    ├── Pravilo indeksacije
                    ├── Priloge — pogodba, aneksi (§6.12)
                    └── Plačilni pogoji
                          │
                          ▼
                    RAČUN (za obdobje LLLL-MM)
                          ├── Postavke — POSNETEK vrednosti
                          ├── Številka (dodeljena ob potrditvi)
                          ├── PDF (posnetek + kontrolni odtis)
                          ├── eSLOG 2.0 XML
                          └── Dogodki pošiljanja
```

**Načelo posnetka.** Izdani račun je davčni dokument. Vsaka postavka računa hrani vrednosti, kot so veljale ob izdaji — znesek, stopnjo, klavzulo, podpisnika, podatke izdajatelja in prejemnika — plus referenco na pogodbo za sledljivost. Poznejša sprememba pogodbe, šifranta ali podatkov podjetja **ne sme spremeniti nobenega že izdanega računa**.

### 6.2 Šifrant DDV režimov

DDV režim je urejljiv šifrant v bazi, ne zakodiran nabor.

| Polje | Opis |
|---|---|
| `sifra` | Interna oznaka |
| `naziv` | Prikazno ime |
| `nacin_obracuna` | Zaprt nabor — glej spodaj |
| `stopnja` | Odstotek |
| `klavzula` | Besedilo na računu |
| `rekapitulacijska_skupina` | Oznaka za seštevanje v rekapitulaciji |
| `zahteva_referenco` | Ali je obvezna referenca listine |
| `velja_od` / `velja_do` | Obdobje veljavnosti |
| `eslog_koda` | Davčna kategorija v eSLOG |
| `aktiven` | Ali je na voljo pri novih vnosih |

**Zaprt nabor načinov obračuna** (v kodi, ne v šifrantu):

| Način | Obnašanje |
|---|---|
| `OBDAVCENO` | Osnova × stopnja = DDV; ločena vrstica rekapitulacije po stopnji |
| `OPROSCENO` | DDV = 0; ločena vrstica rekapitulacije; obvezna klavzula |
| `NI_PREDMET` | Ni v davčni rekapitulaciji; le v skupnem znesku |
| `OBRNJENA_DAVCNA` | DDV = 0; obvezna klavzula o prenosu obveznosti |

Uporabnik lahko doda, uredi ali deaktivira **vrstico** šifranta. Ne more dodati novega **načina obračuna** — to bi pomenilo novo logiko brez testov. Peti način je sprememba kode.

**Zakaj štirje zadoščajo.** Razlika med oprostitvijo brez pravice do odbitka (44. člen) in s pravico (46., 52. člen) je v poročanju, ne v izračunu — obe dasta 0 %. Rešuje jo `rekapitulacijska_skupina` in `eslog_koda` na vrstici šifranta.

**Zavestno ni pokrito:** maržna ureditev (drugačna davčna osnova), pavšalno nadomestilo za kmete, računi za predplačilo (to je tip dokumenta, ne davčni način).

Privzeta vsebina šifranta ob namestitvi je v **Dodatku A**.

### 6.3 DDV pri najemu — poslovni okvir

| Predmet | Privzeti režim | Podlaga |
|---|---|---|
| Najem poslovnega prostora **z izjavo** | `OBDAVCENO_45` (22 %) | 45. člen ZDDV-1; obojestranska izjava DDV-Iz prek eDavkov |
| Najem poslovnega prostora **brez izjave** | `OPROSCENO_44_2` (0 %) | 2. točka 44. člena ZDDV-1 |
| Parkirna mesta in garaže | `OBDAVCENO_STANDARD` (22 %) | Izvzeto iz oprostitve po 44. členu — **ne glede na izjavo** |
| Trajno instalirana oprema in stroji | `OBDAVCENO_STANDARD` (22 %) | Izvzeto iz oprostitve po 44. členu |

**Posledica:** en račun je lahko mešan. Najemnik s pisarno brez izjave in dvema parkirnima mestoma ima dve davčni osnovi in mešano rekapitulacijo.

**Kontrola:** pri režimu z `zahteva_referenco = da` je referenca in datum listine obvezno polje. Brez nje aplikacija ne dovoli izdaje.

**Validacija po statusu prejemnika:** režim, ki zahteva status zavezanca s polno pravico do odbitka, ni dovoljen za fizične osebe in nezavezance.

**Kraj obdavčitve:** pri najemu nepremičnine je kraj obdavčitve tam, kjer nepremičnina leži. Najemnik iz tujine dobi slovenski režim; obrnjena davčna obveznost se pri najemnini ne pojavi.

### 6.4 Šifrant storitev

| Polje | Opis |
|---|---|
| `naziv` | Ime storitve |
| `privzeta_cena` | Neobvezno |
| `privzeti_ddv_rezim` | Referenca v šifrant DDV režimov |
| `enota_mere` | kos, ura, m², mesec … |
| `aktivna` | Ali se pojavi v spustnem seznamu |

Spustni seznam vedno vsebuje možnost **"Drugo — vpiši ročno"**. Šifrant je pospešek in vir konsistence, ne omejitev.

### 6.5 Številčna zaporedja

Dve ločeni, neodvisni zaporedji na **vsako podjetje**:

| Zaporedje | Namen | Format | Reset |
|---|---|---|---|
| `NP` | Najemniški računi | `NP-00147` — brez letnice | Nikoli |
| `ST` | Ročne storitvene fakture | `ST-2026-0001` — z letnico | Letno |

- **Storno in dobropis** porabita številko iz **istega zaporedja** kot izvirni dokument, z referenco na izvirnik in oznako tipa.
- Številka se dodeli **ob potrditvi**, ne ob nastanku osnutka.
- Rezervirana številka se nikoli tiho ne uporabi ponovno.
- Dodeljevanje je atomarno tudi izven mesečnega ciklusa.

### 6.6 Podpisniki

Podpisnik se določi na nivoju podjetja in se lahko med petimi podjetji razlikuje.

| Polje | Obnašanje |
|---|---|
| Ime in priimek | Besedilni vnos |
| Skenirani podpis | Nalaganje `JPG` ali `PNG`; tip preverjen po vsebini datoteke |
| Funkcija / naziv | Neobvezno |

Račun hrani referenco na različico podpisa, ki je veljala ob izdaji.

### 6.7 Obračunsko obdobje in sorazmerni obračun

**Obračun vnaprej.** Račun se izda 1. v mesecu za **tekoči** mesec. Obdobje opravljene storitve je vedno **1. do zadnji dan istega meseca**, ne glede na datum izdaje. Račun, izdan 12. marca, se nanaša na 1. 3. – 31. 3.

- Ob zagonu se privzeto ponudi tekoči mesec.
- Sprememba datuma izdaje obdobja storitve ne premakne.
- Rok plačila se računa od datuma izdaje.

**Sorazmerni obračun** pri začetku ali prekinitvi pogodbe sredi meseca:

```
sorazmerni znesek = mesečna najemnina × (dni najema v mesecu / dni v mesecu)
```

- Ključ so koledarski dnevi; imenovalec je dejansko število dni v mesecu (28/29/30/31).
- Pravilo velja **simetrično** za začetek in konec.
- Enako pravilo velja, če se sredi meseca začne ali preneha zaračunavati samo posamezna postavka pogodbe, na primer dodatno parkirno mesto.
- **Ročni popravek je dovoljen**; ob povozu se obvezno zabeleži razlog in izvirno izračunana vrednost.
- Sorazmerni obračun vedno sproži obvezni pregled (§7.2).

### 6.8 Indeksacija najemnine

Najemnina je v pogodbi dogovorjena in se **letno indeksira, če so izpolnjeni pogodbeni pogoji**. Postopek se začne januarja za preteklo leto, ko so znani podatki o inflaciji. Ker je januarski račun ob obračunu vnaprej izdan še pred objavo indeksa, nova vrednost praviloma velja od **1. februarja**.

Na pogodbi se hrani: osnovna najemnina in datum osnove, pogoj za uskladitev, zadnja uporabljena vrednost indeksa, zgodovina vseh uskladitev.

**Januarski postopek** (ločen od mesečnega):

1. Uporabnik vnese objavljeno vrednost indeksa.
2. Aplikacija za vsako pogodbo izračuna, ali je pogoj izpolnjen, in novo najemnino.
3. Prikaz predloga: stara → nova, %, pogoj DA/NE.
4. Paketna potrditev z možnostjo izključitve posameznih pogodb (z razlogom).
5. Zapis uskladitve z datumom učinkovanja.
6. Prvi mesečni ciklus, ki uporablja novo vrednost (praviloma februar), označi vse usklajene pogodbe kot izjeme.
7. Če je indeks vnesen pozneje in je bil vmes že izdan račun po stari vrednosti, aplikacija predlaga ločeno vrstico poračuna na naslednjem računu. Uporabnik jo potrdi ali zavrne z razlogom; že izdani račun se ne spreminja.

Uskladitev je ločen dogodek od izdaje računa in ima svojo revizijsko sled. Razveljavitev je mogoča le, dokler po njej ni bil izdan noben račun.

### 6.9 Vnos in validacija partnerjev

Vnos je ročen. Integracija z zunanjimi registri ni predvidena — pri obsegu ~15 najemnikov strošek presega korist.

**Kontrole ob vnosu:**

| Kontrola | Obnašanje |
|---|---|
| Davčna številka | Preverjanje dolžine in kontrolne števke (modul 11) |
| Matična številka | Preverjanje dolžine in oblike |
| Obveznost | Pravne osebe ni mogoče shraniti brez davčne in matične številke |
| E-naslov | Preverjanje oblike; opozorilo pri podvojenih naslovih |

Aplikacija predvidi vmesnik za poznejšo priključitev registra (AJPES, VIES), brez implementacije v v1.

---

### 6.10 Trajanje in prenehanje pogodbe

**Datum konca in prenehanje sta dve različni stvari.** Prvotni model je imel eno polje "veljavnost od–do", kar zliva načrtovani konec (kdaj se pogodba izteče po dogovoru) z dejanskim dogodkom (kdaj je prenehala). To vodi v dve nasprotni napaki, obe realni:

| Napaka | Posledica |
|---|---|
| Sistem preneha zaračunavati prezgodaj | Najemnik, ki še vedno uporablja prostor, ne dobi računa — izpad prihodka, ki ga nihče ne opazi |
| Sistem zaračunava predolgo | Račun gre bivšemu najemniku — potreben storno, neprijetnost, izguba zaupanja |

#### Tip trajanja

| Polje | Vrednost |
|---|---|
| `tip_trajanja` | `DOLOCEN_CAS` · `NEDOLOCEN_CAS` |
| `datum_zacetka` | Obvezno |
| `datum_konca` | Obvezno pri `DOLOCEN_CAS`, prazno pri `NEDOLOCEN_CAS` |
| `samodejno_podaljsanje` | Da/Ne — samo pri `DOLOCEN_CAS` |
| `obdobje_podaljsanja` | Npr. 12 mesecev — če je samodejno podaljšanje vklopljeno |
| `odpovedni_rok_mesecev` | Informativno; uporabljeno za opozorila |
| `datum_odpovedi` | Izpolni se, ko je odpoved dejansko podana |
| `datum_prenehanja` | Zadnji dan zaračunavanja — izpolni se ob odpovedi |
| `status` | `AKTIVNA` · `ODPOVEDANA` · `PRENEHALA` |

#### Obnašanje sistema

| Tip pogodbe | Kaj sistem naredi |
|---|---|
| **Določen čas, brez samodejnega podaljšanja** | Zaračunavanje se ustavi z `datum_konca`. Opozorilo 60 dni prej na nadzorni plošči, izjema v paketnem pregledu 30 dni prej. |
| **Določen čas, s samodejnim podaljšanjem** | Zaračunavanje se **ne ustavi**. Ob `datum_konca` se sproži izjema: *"Pogodba se je samodejno podaljšala do {nov datum} — potrdi ali odpovej"*. Uporabnik potrdi, sistem zapiše novo obdobje v zgodovino. |
| **Nedoločen čas (do odpovedi)** | Zaračunavanje teče, dokler ni vnesena odpoved. Brez konca ni ustavljanja. |
| **Odpovedana** | Zaračunava se do `datum_prenehanja`, nato preneha. Zadnji mesec je pogosto sorazmeren (§6.7). |

**Zakaj je samodejno podaljšanje potrjen dogodek, ne tiho nadaljevanje.** Večina slovenskih najemnih pogodb za poslovne prostore vsebuje klavzulo o samodejnem podaljšanju, če nobena stran ne odpove v odpovednem roku. Če bi sistem to izvedel tiho, bi uporabnik izgubil edini trenutek, ko bi se lahko odločil za spremembo pogojev ali odpoved. Zato je podaljšanje **izjema, ki zahteva potrditev**, ne samodejni preskok.

#### Letni pregled aktivnih pogodb

Pogodbe za nedoločen čas nimajo naravne točke preverjanja. Zato: **enkrat letno (januarja, skupaj z indeksacijo) opravilo "Pregled aktivnih pogodb"** — seznam vseh `AKTIVNA` pogodb z zadnjim datumom potrditve. Uporabnik potrdi paketno. Namen ni birokracija, ampak preprečiti, da se tri leta zaračunava najemniku, ki se je odselil, in tega nihče ne opazi.

---

### 6.11 Sklic na pogodbo in objekt na računu

**Problem.** Najemnik z več enotami prejme več računov iz istega podjetja, z istim nazivom postavke ("Najemnina") in podobnim zneskom. Njegovo računovodstvo jih ne more razporediti na pravo pogodbo ali stroškovno mesto.

**Rešitev — dve mesti, obe iz strukturiranih podatkov, brez proste konfiguracije.**

**1. Sklic v glavi računa.** Poleg številke računa in datumov:

```
Sklic na pogodbo:   Najemna pogodba št. 12/2024
Objekt:             Poslovna stavba A, Koper
```

Novo polje na pogodbi: **`oznaka_pogodbe`** — dejanska številka iz podpisane pogodbe (npr. `12/2024`), ne interni ID iz baze. To je edini podatek, ki ga najemnikovo računovodstvo lahko primerja s svojim izvodom pogodbe.

**2. Sestavljen opis postavke.** Opis se sestavi iz strukturiranih polj, ne vnese ročno:

```
Najemnina za obdobje 01.08.2026 – 31.08.2026
Pisarna 1, Poslovna stavba A · Pogodba št. 12/2024
```

Sestavni deli: `{naziv postavke} za obdobje {od} – {do}` / `{oznaka enote}, {naziv nepremičnine} · Pogodba št. {oznaka_pogodbe}`.

**Zakaj sestavljeno in ne prosto besedilo.** Prosto besedilo na postavki bi pomenilo, da uporabnik pri vsakem računu odloča, kaj napisati — kar je natanko ročno delo, ki ga aplikacija odpravlja, in vir neenotnosti, ki jo računovodja opazi. Sestavljanje iz polj je univerzalno: deluje za vsako pogodbo brez nastavitev, in če se enota preimenuje, se opis samodejno prilagodi na prihodnjih računih (na že izdanih ne — velja načelo posnetka, §6.1).

**Posledice drugod:**
- **eSLOG:** sklic na pogodbo ima v eSLOG shemi svoje referenčno polje — isti podatek, brez dodatnega dela.
- **Kontrolni dokument (§9.1):** doda se stolpec `oznaka_pogodbe`, da lahko računovodja razporedi račune po pogodbah.
- **Ročne storitvene fakture:** nimajo pogodbe; polje ostane prazno, objekt se lahko izbere neobvezno.

---

### 6.12 Priloge k pogodbi (naložene datoteke)

Na vsako pogodbo je mogoče naložiti dejanske dokumente — podpisano pogodbo, anekse, izjave — da so vedno pri roki za vpogled in tolmačenje. **Nalaganje ni obvezno.**

**⚠ Ne eno polje, ampak seznam dokumentov.** Pogodbe se aneksirajo: sprememba najemnine, podaljšanje, sprememba prostora, pridobitev izjave po 45. členu. Eno polje "naloži pogodbo" bi v roku enega leta pomenilo, da uporabnik prepiše izvirnik z novejšo verzijo ali zlepi tri dokumente v eno datoteko.

| Polje | Vrednost |
|---|---|
| `tip_dokumenta` | Osnovna pogodba · Aneks · Zapisnik o primopredaji · Izjava DDV-Iz · Drugo |
| `datum_dokumenta` | Datum podpisa, **ne** datum nalaganja |
| `datoteka` | PDF ali DOCX; tip preverjen po vsebini, ne po končnici |
| `opomba` | Prosto besedilo, neobvezno |

**Prikaz:** ikona s številom priloženih dokumentov na pogodbi (npr. 📎 3), klik odpre seznam. Ikona je prisotna na **dveh** mestih:
1. Seznam in obrazec pogodbe — za brskanje.
2. **Zaslon obravnave izjeme** — najpomembnejše mesto. Ko paketni pregled pokaže *"Pogodba poteče 31. 8."* ali sorazmerni obračun, se uporabnik odloča in takrat potrebuje pogodbo en klik stran, ne tri.

**Varnostna kopija — višja prioriteta kot računi.** To je bistvena razlika:

| | Izdan PDF računa | Naložena pogodba |
|---|---|---|
| Narava | **Generiran artefakt** | **Izvorni dokument** |
| Če se izgubi | Ponovno se zgenerira iz podatkov v bazi | **Izgubljen dokončno** (razen papirnega izvirnika) |

Zato: naložene priloge gredo v Dropbox posnetek (§11.3) **prednostno**, in ob obnovi iz posnetka se preveri, da so vse referencirane datoteke prisotne. Manjkajoč generiran PDF je nadloga; manjkajoča skenirana pogodba je izguba.

**Tehnični zahtevi:**
- Datoteka se streže prek nadzorovanega dostopa, ne kot statična pot — sicer je uporabnikovo ime datoteke del naslova.
- Brisanje naložene priloge se zabeleži v revizijsko sled (kdo, kdaj, katera datoteka), ker gre za izvorni dokument.

**Prioriteta: P1.** Ne blokira izdaje računov. Smiselno izvesti v fazi 3, skupaj z ostalimi zasloni za pogodbe.

---

### 6.13 Prehod iz Excela v aplikacijo

Ob prvem prehodu se ne uvaža celotnega arhiva starih računov. Za vsako obstoječo pogodbo se vnese trenutno veljavno stanje na zadnji mesec, ki je bil dejansko obračunan v Excelu:

- trenutni zneski in DDV režimi postavk,
- zadnja uporabljena vrednost indeksa in datum te vrednosti,
- zadnja uporabljena številka zaporedja `NP` in `ST` za vsako podjetje,
- veljavni podpisnik, veljavna uporabnikova potrditev DDV-nastavitev in veljavna uporabnikova potrditev vzorca podjetja.

Aplikacija iz teh podatkov ustvari **tehnični referenčni zapis** za primerjavo v prvem pravem mesecu. Ta zapis ni nov izdan račun, nima številke in se ne pošilja; služi samo temu, da prvi mesec ne prikaže vseh obstoječih pogodb kot novih. Referenčni mesec je vedno zadnji dejansko obračunani mesec pred prehodom, ne nujno december.

---

## 7. Mesečni postopek

### 7.1 Načelo pregleda

Če je znesek izpeljan iz pogodbe in ne prepisan, pregled PDF-ja ne preverja ničesar, česar stroj ne bi. Kar pregled še ujame, so **spremembe**. Zato se pregled preusmeri z "vsakega računa" na "vsako spremembo".

### 7.2 Sprožilci obveznega pregleda

Račun se označi kot **izjema** in zahteva posamično potrditev, če velja karkoli od:

1. znesek se razlikuje od zadnjega dejansko izdanega računa,
2. prva izdaja po tej pogodbi,
3. pogodba se izteče znotraj obračunskega obdobja ali v naslednjih 30 dneh,
3a. pogodba z določenim časom in samodejnim podaljšanjem je dosegla `datum_konca` — zahteva potrditev podaljšanja,
3b. pogodba je odpovedana in je to zadnji ali predzadnji mesec zaračunavanja,
4. spremenjeni matični podatki najemnika ali izdajatelja od zadnje izdaje,
5. spremenjen DDV režim ali manjkajoča obvezna referenca listine,
6. sorazmerni obračun,
7. pogodba ima manjkajoče obvezno polje,
8. prejšnji mesec je bil ta račun arhiviran kot neizdan.

**Sprejemno merilo:** noben račun s katerimkoli sprožilcem ne more biti poslan brez posamezne potrditve. Nasprotno: račun brez sprožilca ne sme zahtevati posamezne potrditve.

### 7.3 Paketni pregled

Ena tabela, vse vrstice, primerjava z zadnjim dejansko izdanim računom:

```
Podjetje   Najemnik      Enota        Prejšnji    Ta mesec   Δ      Status
─────────────────────────────────────────────────────────────────────────
A d.o.o.   Alfa d.o.o.   Pisarna 1    1.200,00    1.200,00   —      ✓ Enako
A d.o.o.   Beta s.p.     Skladišče      850,00      874,55   +2,9%  ⚠ Indeksacija
A d.o.o.   Gama d.o.o.   Pisarna 3    1.500,00         —      —     ⚠ Pogodba poteče
B d.o.o.   Delta d.o.o.  Lokal 2      2.100,00    2.100,00   —      ⚠ Spremenjen e-naslov
B d.o.o.   Epsilon       Parkirišče        —        120,00   nova   ⚠ Prva izdaja
```

Vrstice `✓ Enako` so privzeto potrjene. Vrstice `⚠` zahtevajo posamično potrditev; klik odpre PDF in razlog odstopanja.

### 7.4 Zaporedje korakov

| # | Korak | Vsebina |
|---|---|---|
| 1 | Izbira meseca | Datum izdaje, obdobje storitve, zapadlost — privzeto iz pogodb |
| 2 | Predhodna kontrola | Manjkajoči podatki, potekle pogodbe, nepotrjene reference listin; na ravni podjetja tudi veljavni podpisnik ter veljavni uporabnikovi potrditvi DDV-nastavitev in vzorca |
| 3 | Izračun osnutkov | Deterministično iz pogodb; idempotentno |
| 4 | Paketni pregled | Ena tabela, Δ proti zadnjemu dejansko izdanemu računu |
| 5 | Obravnava izjem | Samo označene vrstice, posamično |
| 6 | Potrditev paketa | Dodelitev številk, generiranje PDF + eSLOG, zaklep |
| 7 | Pošiljanje in preverjanje | Iz pravega predala; TO/CC/BCC; BCC računovodja; preverjanje odgovora odhodnega e-poštnega strežnika za vsak račun |
| 8 | Zaključek | Arhiv, evidenca, zbirni rezultat, seznam poslanih / neizdanih / neuspešnih ter obvestilo uporabniku |

Vsak račun se ob potrditvi zaklene in oštevilči v svoji atomarni transakciji. Če se postopek prekine sredi paketa, že potrjeni računi ostanejo veljavni, preostali pa ostanejo osnutki in se lahko varno nadaljujejo pozneje.

V uporabniškem korakovniku je osem operativnih korakov združenih v šest vidnih korakov (§16.1): paketni pregled in obravnava izjem sta en korak **Pregled**, pošiljanje, preverjanje, arhiviranje in zbirno obvestilo pa so del koraka **Zaključek**. Zelena kljukica se prikaže šele, ko so uspešno izvedena vsa opravila znotraj vidnega koraka.

### 7.5 Statusi dokumentov

| Status | Pomen |
|---|---|
| `Osnutek` | Pripravljen, ni potrjen |
| `Za ponovni pregled` | Po potrditvi spremenjen — vidno označen |
| `Izbran za pošiljanje` | Potrjen posamično ali paketno |
| `Poslan` | Odhodni e-poštni strežnik je sporočilo sprejel |
| `Napaka` | Neuspeh je potrjen: strežnik je sporočilo zavrnil ali je povezava odpovedala, preden je bilo sporočilo oddano; shranjena sta razlog in varen ponovni poskus |
| `Rezultat ni znan – potrebno preverjanje` | Oddaja se je začela, vendar aplikacija ni prejela končne potrditve strežnika; samodejni ponovni poskus ni dovoljen |
| `Arhiviran – ni izdan` | Ta mesec ni poslan; z razlogom; obnovljiv |
| `Storniran` / `Popravljen` | Povezan z izvirnikom in zgodovino |

Varen ponovni poskus pri potrjeni napaki uporabi **isti potrjeni PDF, isti eSLOG, isto številko in iste podatke računa**. Ne ustvari novega računa in ne ponovi uspešno poslanih sporočil; v revizijski sledi ohrani vsak poskus.

Če je rezultat neznan, aplikacija računa ne pošlje samodejno znova. Najprej preveri mapo Poslano, kadar povezani ponudnik in dovoljenja to omogočajo. Če samodejno preverjanje ni mogoče, uporabniku pokaže navodilo za ročno preverjanje in zahteva izrecno odločitev. Če je sporočilo najdeno, se račun označi kot `Poslan`; če uporabnik potrdi, da ni bilo poslano, se omogoči varen ponovni poskus. Preverjanje in uporabnikova odločitev se zabeležita v revizijski sledi.

### 7.6 Zaključna kontrola pošiljanja in obvestilo uporabniku

Ko so izvedeni vsi poskusi pošiljanja v mesečnem paketu, aplikacija preveri rezultat za vsak račun posebej. **Uspešno poslano** pomeni, da je odhodni e-poštni strežnik sporočilo sprejel. To ne pomeni potrditve, da je sporočilo prispelo v prejemnikov nabiralnik ali da ga je prejemnik odprl.

Če se povezava prekine po začetku oddaje sporočila, vendar pred prejemom končne potrditve strežnika, rezultat ni obravnavan kot navadna napaka. Račun dobi status `Rezultat ni znan – potrebno preverjanje`, ostane odprta naloga na zaključnem zaslonu in nadzorni plošči ter se vključi v zbirno poročilo uporabniku.

Za vsak poskus se v evidenco shranijo čas, podjetje, številka računa, pošiljatelj, prejemniki in rezultat strežnika. Ob napaki ali neznanem rezultatu se prikažejo:

- podjetje, številka računa in prejemnik,
- korak, pri katerem je prišlo do napake,
- razumljiv opis napake,
- tehnična koda in prečiščen odgovor strežnika, brez gesel ali drugih skrivnosti,
- dovoljen naslednji korak: varen ponovni poskus pri potrjeni napaki oziroma preverjanje mape Poslano ali uporabnikova odločitev pri neznanem rezultatu.

Napake, kot so napačen naslov prejemnika, zavrnjena prijava, nedosegljiv strežnik, prekinjena povezava ali prekoračen čas čakanja, morajo biti med seboj jasno ločene. Splošno sporočilo *»Prišlo je do napake«* ni sprejemljivo.

Po zaključni kontroli aplikacija sestavi **eno zbirno poročilo za celoten mesečni postopek** in ga pošlje na en uporabnikov e-poštni naslov, nastavljen v splošnih nastavitvah aplikacije. Naslov ni vezan na posamezno podjetje. Poročilo vsebuje:

- mesec in čas izvedbe,
- rezultat po posameznem podjetju,
- število pripravljenih, uspešno poslanih, arhiviranih kot neizdanih, neuspešnih računov in računov z neznanim rezultatom,
- stanje arhiviranja,
- pri vsakem neuspehu podjetje, številko računa, prejemnika in razlog napake.

Če ni napak, ima poročilo jasno oznako **»Mesečni postopek uspešno zaključen«**. Če katerikoli zahtevani korak ni uspel, ima oznako **»Mesečni postopek zahteva pozornost«** in navede vse odprte težave. Namenoma arhiviran račun z zapisanim razlogom ni napaka.

Enak rezultat se prikaže v aplikaciji na zaključnem zaslonu in po izhodu iz korakovnika ostane viden na nadzorni plošči. Vsak uspešno zaključen vidni korak ima zeleno kljukico. Korak z napako je označen z rdečim opozorilom, besedilom **»Ni zaključeno«** in natančnim opisom mesta ter vzroka napake; stanje ni izraženo samo z barvo.

Celoten mesečni postopek dobi zeleno potrditev samo, če so uspešno končana vsa zahtevana opravila in noben račun nima neznanega rezultata. Ob delnem uspehu dokončani koraki ohranijo zelene kljukice, neuspešni ali nepreverjeni korak pa ostane odprt do razrešitve. Če ne uspe samo pošiljanje zbirnega poročila uporabniku, to ne spremeni že potrjenih statusov računov; aplikacija napako prikaže posebej in omogoči ponovni poskus pošiljanja poročila.

---

## 8. Ročne storitvene fakture

Izdaja kadarkoli med mesecem, izven mesečnega ciklusa.

| Element | Obnašanje |
|---|---|
| Naziv storitve | Izbor iz šifranta ali prost vnos |
| Vrednost | **Vnos z DDV (bruto)**; aplikacija izračuna osnovo in DDV nazaj |
| DDV režim | Iz šifranta storitve ali ročni izbor |
| Prejemnik | Iz obstoječih najemnikov ali nov vnos |
| Izdajatelj | Izbor med 5 podjetji |
| Številčenje | Zaporedje `ST` |
| Pregled | Vedno obvezen posamični pregled |

**Izračun nazaj iz bruto zneska.** Osnova in DDV se izračunata tako, da je njuna vsota **natanko enaka** vnesenemu bruto znesku. Naivna implementacija (zaokroži osnovo, zaokroži DDV, seštej) da pri nekaterih zneskih razliko enega centa.

---

## 9. Kontrolni dokument za računovodjo

Poleg BCC kopije vsakega računa se računovodji pošlje rekapitulacijski dokument za pretekli mesec, **ločeno za vsako podjetje**. BCC daje dokumente; kontrolni dokument omogoča preverjanje, da so vsi in da se ujemajo.

### 9.1 Vsebina

| Sklop | Polja |
|---|---|
| Glava | Podjetje (naziv, davčna, matična), obdobje, datum izdelave |
| Vrstice | Številka · tip (`NP`/`ST`/`STORNO`/`DOBROPIS`) · prejemnik · davčna · matična · datum izdaje · obdobje storitve · DDV režim · osnova · DDV · skupaj |
| Rekapitulacija | Vsota po rekapitulacijskih skupinah in stopnjah |
| Neizdano | Pogodbe, ki ta mesec niso bile zaračunane, z razlogom |

Razdelek "Neizdano" omogoča ločevanje med "najemnik ne plačuje" in "račun se je izgubil".

### 9.2 Oblika in dostava

**Ena e-pošta na podjetje, ena priloga.**

| Element | Vrednost |
|---|---|
| Zadeva | `Kontrolni dokument za {Podjetje} - {mesec} - {leto}` |
| Priloga | Ena datoteka, **CSV** |
| Ime datoteke | `{leto}-{mesec}-{podjetje}.csv` (npr. `2026-07-Alfa-doo.csv`) |
| Prejemnik | E-naslov računovodje, nastavljen na nivoju podjetja |
| Arhiv | Kot datoteka, brez številčenja dokumenta |

Mesec v imenu datoteke je dvomestna številka (razvrščanje), mesec v zadevi e-pošte je ime (berljivost).

**CSV shema — slovenska lokalizacija:**

| Parameter | Vrednost |
|---|---|
| Ločilo | podpičje `;` |
| Decimalno ločilo | vejica |
| Ločilo tisočic | brez |
| Kodiranje | UTF-8 **z BOM** |
| Konec vrstice | CRLF |
| Datumi | `dd.mm.llll` |
| Glava | Prva vrstica z imeni stolpcev |

Stolpci: `vrstica_tip; stevilka_racuna; tip; prejemnik; davcna_stevilka; maticna_stevilka; oznaka_pogodbe; datum_izdaje; obdobje_od; obdobje_do; ddv_rezim; osnova; ddv; skupaj`

`vrstica_tip` ima vrednosti `RACUN`, `REKAPITULACIJA`, `NEIZDANO` — ena datoteka, ena shema.

Aplikacija poleg CSV arhivira **PDF izvod lokalno** kot fiksen posnetek poročanega.

### 9.3 Sprožilec

Opravilo na nadzorni plošči, **ne pogovorno okno**.

| Stanje | Kdaj |
|---|---|
| `Odprto` | Od 1. v mesecu do roka |
| `Delno` | Poslano za nekatera podjetja |
| `Opozorilo` | Po roku (privzeto 5. v mesecu), če ni poslano za vsa |
| `Zaključeno` | Poslano za vsa — opravilo izgine |
| `Zastarelo` | Nov dokument z datumom v že poročanem mesecu |

Rok je nastavljiv. Če pade na soboto, nedeljo ali praznik, opozorilno stanje nastopi naslednji delovni dan. Opravilo v nobenem stanju ne blokira dela.

### 9.4 Izpis za poljubno obdobje

Ista shema in rekapitulacija, izbira obdobja od–do in enega ali več podjetij. Ne šteje kot poslani mesečni dokument. Ime datoteke: `{od}_{do}-{podjetje}.csv`.

---

## 10. E-račun in regulativa

**Dejstvo.** Zakon o izmenjavi elektronskih računov in drugih elektronskih dokumentov (ZIERDED) se uporablja od **1. januarja 2028** in uvaja obvezno izmenjavo e-računov med podjetji. Račun v PDF obliki je elektronski račun, **ni pa e-račun** — zahteva se strukturiran zapis (eSLOG 2.0) prek ponudnika e-poti, vpisanega na seznam pri UJP.

*Zanesljivost: visoka za obstoj obveznosti, srednja za točen datum — v osnutku zakona je bil rok 1. 1. 2027. Uporabnik pred produkcijsko uporabo preveri veljavni uradni vir.*

**Arhitekturna posledica.** Račun se ne modelira kot "PDF z vsebino", ampak kot strukturiran zapis, iz katerega se PDF **renderira**. eSLOG XML je drugi renderer istega zapisa.

### 10.1 Dostavni kanal

| Kanal | Vsebina |
|---|---|
| `PDF` | PDF v prilogi e-pošte |
| `ESLOG` | eSLOG 2.0 XML v prilogi e-pošte |
| `OBOJE` | Oboje v isti e-pošti |

Kanal je **privzet na nivoju podjetja in povozljiv na nivoju najemnika**, ker je zmožnost prejemnika, ne odločitev izdajatelja.

Za prejemnike, označene kot fizične osebe, je kanal `ESLOG` onemogočen.

### 10.2 Prejemniki po kanalu

Kontakt najemnika je označljiv za `PDF`, `ESLOG` ali oba. Isti kontakt je lahko označen za oba.

eSLOG prejemnik se ne modelira kot e-poštni naslov, ampak kot zapis z:
- davčno in matično številko (obvezno za XML),
- e-naslovom za prehodno obdobje,
- praznim mestom za naslov e-poti (v2).

**Razmejitev:** pošiljanje eSLOG XML kot priloge e-pošte ni skladna izmenjava po ZIERDED. Je uporabna prehodna praksa in pravi korak zdaj, ne pa izpolnjena obveznost za 2028.

### 10.3 Konfiguracija segmentov

- Obvezni segmenti izhajajo iz **XSD sheme** in so v vmesniku zaklenjeni.
- Neobvezni segmenti so označljivi; privzeto na podjetju, povozljivo na prejemniku.
- Neobvezna polja se modelirajo kot **preslikava ključ→vrednost na nivoju najemnika**, ne kot fiksni stolpci.
- Jamstvo je **validacija generiranega XML proti shemi** pred pošiljanjem, ne vmesnik.

### 10.4 Prioriteta

Ker eSLOG danes ne zahteva noben najemnik:

- **P0:** XML se generira, validira in arhivira ob vsakem računu.
- **P1:** XML se dejansko pošilja; konfiguracija segmentov ima vmesnik.

Generiranje brez pošiljanja nič ne stane in v celoti pripravi. Ob prvi zahtevi je XML že arhiviran za nazaj.

---

## 11. Hramba, arhiv in varnostne kopije

### 11.1 Tri lokacije, tri funkcije

| Lokacija | Funkcija | Pred čim ščiti |
|---|---|---|
| Lokalno | Delovna kopija | — |
| iCloud | Razpoložljivost in dostop | Odpoved diska ali naprave |
| Dropbox (posnetki) | **Varnostna kopija** | Izbris, okvara, napačen prepis |

### 11.2 Arhiv: lokalno + iCloud

```
/Arhiv/{Podjetje}/{Leto}/{Mesec}/{Stevilka}.pdf
                                 {Stevilka}.xml
```

Ista struktura na obeh lokacijah. Baza hrani obe poti in en kontrolni odtis (SHA-256), ki mora ustrezati obema datotekama.

Račun se označi kot arhiviran šele, ko sta obe pisanji potrjeni in odtisa preverjena. Če iCloud ni dosegljiv, račun ostane v stanju `Arhiviranje v teku`; pošiljanje se ne blokira.

**Opozorilo:** iCloud Drive je replikacija, ne varnostna kopija. Izbris ali okvara se razširi.

### 11.3 Varnostna kopija: Dropbox

V Dropbox mapo se piše **datirani posnetek**, ne živa struktura.

```
/Dropbox/RacuniBackup/
    2026-08-01_backup.zip      ← baza + arhiv + logotipi + podpisi + priloge k pogodbam
    2026-09-01_backup.zip
```

- Posnetek pred in po vsakem mesečnem postopku ter po urniku.
- Več generacij (predlog: 12 mesečnih + zadnjih 5 dnevnih).
- Ob izdelavi se preveri odprtje ZIP in kontrolni odtis baze.
- **Naložene priloge k pogodbam (§6.12) imajo prednost** — ob obnovi se preveri, da so vse referencirane datoteke prisotne. Generiran PDF računa se da poustvariti iz baze; skenirana pogodba ne.
- Izbris žive datoteke posnetka ne prizadene.

Ob predaji se izvede **dejanska obnova iz posnetka**, ne le izdelava.

---

## 12. Zahteve

### 12.1 P0 — brez tega ni izdelka

| # | Zahteva | Sprejemno merilo |
|---|---|---|
| P0-1 | Upravljanje 5 izdajateljev | Vsako podjetje ima svoji zaporedji, podpisnika, logotip, IBAN, e-poštni predal, e-naslov računovodje |
| P0-2 | Najemne pogodbe z enotami in postavkami | Pogodba vsebuje najemnika, enoto, postavke z DDV režimom, veljavnost, rok, prejemnike |
| P0-3 | Deterministični izračun osnutkov | Dvakratni zagon istega meseca da identičen rezultat brez dvojnikov |
| P0-4 | Mešani DDV na enem računu | Postavki `OPROSCENO` in `OBDAVCENO` dasta dve davčni osnovi in pravilno rekapitulacijo |
| P0-5 | Šifrant DDV režimov | Dodajanje, urejanje, deaktiviranje vrstic; `nacin_obracuna` ni urejljiv |
| P0-6 | Zgodovinskost šifrantov | Sprememba stopnje ali klavzule ne spremeni nobenega izdanega računa |
| P0-7 | Klavzula na računu | Ob oproščeni postavki se izpiše zakonska klavzula iz šifranta |
| P0-8 | Obvezna referenca listine | Režim z `zahteva_referenco` brez reference blokira izdajo |
| P0-9 | Validacija režima po statusu prejemnika | Režim za zavezance ni dovoljen za fizične osebe in nezavezance |
| P0-10 | Šifrant storitev s prostim vnosom | Spustni seznam + "Drugo"; izbira prednastavi DDV režim in enoto |
| P0-11 | Paketni pregled z Δ primerjavo | Zaslon §7.3 primerja z zadnjim dejansko izdanim računom; sprožilci §7.2 delujejo brez izjeme |
| P0-12 | Ločeni zaporedji `NP` in `ST` | Izdaja `ST` med pripravo `NP` paketa ne vpliva na `NP` zaporedje |
| P0-13 | Pravila resetiranja | Test prehoda 31. 12. → 1. 1. dokaže reset `ST` in ne-reset `NP` |
| P0-14 | Atomarno dodeljevanje številk | Prekinitev ne pusti luknje ali dvojnika |
| P0-15 | Obračun vnaprej | Obdobje storitve je vedno cel koledarski mesec; datum izdaje ga ne premakne |
| P0-16 | Sorazmerni obračun | Koledarski dnevi za pogodbo ali posamezno postavko; ročni povoz zahteva razlog in ohrani izvirno vrednost |
| P0-17 | Ročna storitvena faktura | Vnos bruto; vsota osnove in DDV natanko enaka vnesenemu znesku |
| P0-18 | Januarska indeksacija | Vnos indeksa → predlog → paketna potrditev z izključitvami; praviloma velja od 1. februarja; zamujen vnos predlaga poračun brez spremembe izdanih računov; revizijski zapis |
| P0-19 | Podpisnik na nivoju podjetja | Ime, priimek, JPG/PNG podpis; tip preverjen po vsebini; posnetek ob izdaji |
| P0-20 | PDF identičen poslanemu in arhiviranemu | Kontrolni odtis se ujema v bazi, arhivu in prilogi |
| P0-21 | eSLOG 2.0 XML, validiran proti XSD | Zneski in številka se ujemajo s PDF do centa; brez validacije ni pošiljanja |
| P0-22 | Dostavni kanal | Privzeto na podjetju, povozljivo na najemniku; `ESLOG` onemogočen za fizične osebe |
| P0-23 | Prejemnik po kanalu | Kontakt označljiv za PDF, eSLOG ali oba; eSLOG prejemnik hrani davčno in matično |
| P0-24 | Pošiljanje iz pravega predala | Poverilnice v Keychainu, nikoli v bazi; test povezave pred prvim pošiljanjem |
| P0-25 | Idempotentno ponovno pošiljanje | Pri potrjeni napaki se ponovno pošljejo isti PDF, eSLOG, številka in podatki računa; nov račun ne nastane in uspešno poslana sporočila se ne ponovijo |
| P0-26 | Revizijska sled | Za vsako spremembo: čas, polje, stara vrednost, nova vrednost, razlog |
| P0-27 | Storno in dobropis | Izdani račun se ne briše in ne popravlja; številka iz istega zaporedja z referenco |
| P0-28 | Lokalna validacija davčne in matične | Napačna kontrolna števka blokira shranjevanje pravne osebe |
| P0-29 | Dvotirni arhiv lokalno + iCloud | Obe pisanji potrjeni in odtisa preverjena pred oznako "arhivirano" |
| P0-30 | Zaznava razhajanja arhiva | Manjkajoča ali spremenjena datoteka sproži opozorilo |
| P0-31 | Varnostna kopija z obnovo | Datirani posnetki v Dropbox; obnova dejansko izvedena ob predaji |
| P0-32 | Kontrolni dokument za računovodjo | CSV po §9; ena e-pošta na podjetje z eno prilogo |
| P0-33 | CSV po slovenski lokalizaciji | Datoteka se v Excelu odpre v ločene stolpce s pravilnimi šumniki in števili |
| P0-34 | Opravilo za kontrolni dokument | Stanja §9.3; po roku opozorilo; nikoli ne blokira |
| P0-35 | Zaznava zastarelega kontrolnega dokumenta | Nov dokument v poročanem mesecu ponovno aktivira opravilo |
| P0-36 | Izpis za poljubno obdobje | Izbira od–do in podjetij; ne šteje kot poslani mesečni dokument |
| P0-37 | Opravilo "Potrditev DDV režimov" | Blokira prvo produkcijsko izdajo, dokler uporabnik ne potrdi razporeditve za prizadeto podjetje |
| P0-38 | Tip trajanja pogodbe | `DOLOCEN_CAS` z obveznim `datum_konca`, `NEDOLOCEN_CAS` brez; zaračunavanje se ustavi samo tam, kjer je konec dejansko določen |
| P0-39 | Samodejno podaljšanje kot potrjen dogodek | Pogodba s samodejnim podaljšanjem ob `datum_konca` sproži izjemo, ne tihega nadaljevanja in ne tihe ustavitve |
| P0-40 | Odpoved pogodbe | Vnos `datum_odpovedi` in `datum_prenehanja`; zaračunavanje preneha po `datum_prenehanja`, zadnji mesec sorazmerno če ni celoten |
| P0-41 | Oznaka pogodbe na računu | Polje `oznaka_pogodbe` se izpiše v glavi računa kot sklic; prisotno tudi v eSLOG in v kontrolnem dokumentu |
| P0-42 | Sestavljen opis postavke | Opis vsebuje obdobje, oznako enote, naziv nepremičnine in oznako pogodbe; sestavljen iz polj, ne ročno vnesen |
| P0-43 | Pojasnila pri poljih z visokimi posledicami | DDV režim, referenca izjave, tip trajanja, samodejno podaljšanje, pogoj indeksacije, dostavni kanal in rekapitulacijska skupina imajo trajno pojasnilo, vidno brez prehoda miške (§16.2) |
| P0-44 | Operativni priročnik s preizkušeno obnovo | Priročnik (§16.3) obstaja **in** je bila obnova iz varnostne kopije izvedena po njegovem postopku pred predajo |
| P0-45 | Navigacija na štirih postavkah | Prva raven: Nadzorna plošča · Pogodbe · Arhiv · Nastavitve. Mesečni ciklus se ne pojavi v meniju (§16.1) |
| P0-46 | Korakovnik v mesečnem ciklusu | Vseh šest korakov je vidnih ves čas; trenutni je poudarjen, uspešni imajo zeleno kljukico, neuspešni rdeče opozorilo, besedilo »Ni zaključeno« ter natančen opis mesta in vzroka napake |
| P0-47 | Ohranjanje stanja ciklusa | Zaprt in ponovno odprt brskalnik sredi ciklusa vrne uporabnika na isti korak z istimi podatki |
| P0-48 | Tabelarne številke in poravnava | Zneski v paketnem pregledu, kontrolnem dokumentu in na računu desno poravnani, z navpično poravnanimi decimalkami |
| P0-49 | Zaključna kontrola pošiljanja | Za vsak izbran račun obstaja rezultat odhodnega strežnika; sprejet odgovor pomeni `Poslan`, potrjen neuspeh pa `Napaka` z razlogom in varnim ponovnim poskusom |
| P0-50 | Zbirno obvestilo uporabniku | Po mesečnem postopku se eno poročilo za vseh pet podjetij pošlje na en uporabnikov e-naslov iz splošnih nastavitev in enak rezultat ostane viden v aplikaciji; napaka pošiljanja poročila ne spremeni statusov računov |
| P0-51 | Obravnava neznanega rezultata pošiljanja | Prekinitev po začetku oddaje brez končne potrditve ustvari status `Rezultat ni znan – potrebno preverjanje`; samodejni ponovni poskus je blokiran, dokler aplikacija ne preveri mape Poslano ali uporabnik izrecno ne potrdi, da sporočilo ni bilo poslano |
| P0-52 | Čarovnik prvega zagona | Uporabnika vodi po zaporedju podjetje → nepremičnina → enota → najemnik → pogodba → pogodbene postavke ter preveri vse obvezne podatke, potrebne za izdelavo vzorčnega računa. Pred zaključkom uspešno izvede neuničujoč preizkus dejanskega zapisa in ponovnega branja začasne datoteke v lokalnem arhivu, iCloud arhivu in Dropbox mapi za varnostne posnetke ter začasne datoteke po uspešnem preverjanju odstrani. Neuspeh katerekoli lokacije blokira zaključek začetne nastavitve in produkcijsko izdajo. |
| P0-53 | Vzorčni račun za vsako podjetje | Za vsako od petih podjetij uporabnik izbere eno veljavno pogodbo, pregleda dejanski videz PDF-ja in ga izrecno potrdi; vzorec je označen »VZOREC – NI RAČUN«, nima in ne porabi uradne številke, se ne pošlje ter se ne shrani v uradni arhiv računov |
| P0-54 | Ločeni uporabnikovi potrditvi | Potrditev DDV-nastavitev in potrditev vzorca sta shranjeni ločeno za posamezno podjetje; sprememba DDV-nastavitev razveljavi obe, računovodsko pomembna sprememba ali sprememba PDF-predloge razveljavi potrditev vzorca, manjša vizualna sprememba pa ne razveljavi potrditve DDV |
| P0-55 | Poznejše vzdrževanje začetnih podatkov | Podjetja, nepremičnine, enote, najemnike, pogodbe in postavke je mogoče pozneje dodajati, urejati, arhivirati in ponovno aktivirati, kadar je to dovoljeno. Fizični izbris je dovoljen samo za še nikoli uporabljen zapis brez poslovnih, podatkovnih ali podrejenih referenc; sicer aplikacija izbris zavrne z jasnim razlogom in ponudi arhiviranje. Pred dovoljenim izbrisom zahteva izrecno potrditev ter ohrani minimalno revizijsko sled vrste zapisa, časa in dejanja uporabnika. |
| P1-a | Delo s tipkovnico v paketnem pregledu | Premik po vrsticah, potrditev, nadaljevanje brez miške |

### 12.2 P1 — hitri nasledniki

- Nadzorna plošča: števci po statusih, stanje kopije, opozorilo na potekajoče pogodbe.
- Iskanje in filtriranje arhiva (podjetje, najemnik, mesec, številka, status).
- Kopiranje pogodbe kot izhodišče za novo.
- **Preneseno v P0-55 v v1.2:** arhiviranje in ponovna aktivacija najemnika ter pogodbe sta del skupnih pravil življenjskega cikla vseh začetnih podatkov.
- Pošiljanje eSLOG XML in vmesnik za konfiguracijo segmentov.
- Predogled PDF na zahtevo.
- Preverjanje ID za DDV (VIES) kot kontrola predpogoja za režim.
- **Priloge k pogodbi** (§6.12): nalaganje PDF/DOCX dokumentov, seznam po tipih, ikona z dostopom iz obrazca pogodbe in iz zaslona obravnave izjeme; vključeno v varnostno kopijo prednostno.
- **Letni pregled aktivnih pogodb** (§6.10): januarsko opravilo, paketna potrditev, da se pogodbe za nedoločen čas ne zaračunavajo v prazno.

### 12.3 P2 — arhitekturno predvideti, ne graditi

- Oddaja e-računov prek ponudnika e-poti.
- Samodejni prenos indeksa cen življenjskih potrebščin.
- Spremljanje plačil iz bančnega izpiska.
- Priključitev registra poslovnih subjektov (vmesnik obstaja od v1).

---

## 13. Tehnični okvir

Podrobnosti in utemeljitev izbire so v ločenem dokumentu **SAD (Arhitekturni načrt)**; tu le okvir, ki je del obsega.

| Plast | Izbira |
|---|---|
| Platforma | macOS, ena naprava, lokalni strežnik na `127.0.0.1`, brez omrežne izpostavljenosti |
| Jezik | Python 3.12+ |
| Ogrodje | Django 5.x |
| Uporabniški vmesnik | Django templates + HTMX (dostop prek brskalnika) |
| Baza | SQLite, lokalna |
| PDF | WeasyPrint (HTML/CSS → PDF) |
| eSLOG XML | lxml |
| Poverilnice | `keyring` (samodejno uporabi macOS Keychain) |
| Namestitev | `venv` neposredno na macOS, brez Dockerja |

**Zakaj ta izbira.** Odločilna prednost je Django admin, ki pokrije velik del P0 obsega vzdrževanja matičnih podatkov (podjetja, pogodbe, šifranti) brez ročnega pisanja obrazcev. Dodatno: `keyring` nadomesti ročno Keychain integracijo z eno vrstico namesto platformno specifične kode; brez notarizacije in podpisovanja za macOS distribucijo, ker gre za eno napravo, ne distribucijo tretjim; Python/Django je bistveno bolje zastopan v učnih podatkih AI orodij kot Avalonia, kar je pomembno za razvijalca, ki se z AI podporo šele uči.

**Zavesten kompromis glede cilja C5 (podatkovna suverenost).** Django je spletno ogrodje, uporabljeno tu za enouporabniško lokalno rabo. Podatki ostajajo v celoti na napravi — nič ne zapusti MacBooka — a arhitektura uvede HTTP kot posrednik med uporabnikom in podatki, ki ga native aplikacija ne bi imela. To je zavestno sprejet kompromis, ne spregled; razlogi zanj so pretehtani v SAD.

**Varnostna pravila.** Ker je aplikacija tehnično spletna (čeprav samo na `127.0.0.1`), CSRF zaščita in ubežanje uporabniškega vnosa (XSS) sta aktivna kot del standardnega Django obnašanja. Avtentikacija, avtorizacija, CORS, rate limiting, brute-force zaščita in TLS ostanejo izključeni — glej SAD §2.1 za polno utemeljitev.

**Modularna struktura** — obvezna zahteva naročnika, prevedena na standardne Django aplikacije (podrobno v SAD §3). Domenska in podatkovna plast sta znotraj posamezne Django aplikacije, ne ločeni vrhnji mapi:

```
companies/      models.py (podatki) + services.py (poslovna pravila)
catalogs/       models.py + services.py
contracts/      models.py + services.py
invoicing/      models.py + services.py
rendering/      PDF in eSLOG rendererji
delivery/       e-pošta, ponovni poskusi, keyring
archive/        arhiv, kontrolni odtisi, kopije, obnova
ui/             Django views, templates, forms, HTMX fragmenti
app/            Django settings, URL usmerjanje, zagon
```

Domenske storitve v `services.py` se ne smejo sklicevati na Django views, templates ali karkoli iz `ui/`. Njihova testabilnost brez zagnanega strežnika je merilo pravilnosti razdelitve — test mora teči z `pytest`, brez `runserver`.

**Poslovna logika ni v Django modelih.** V nasprotju z običajno Django konvencijo ("fat models") ostajajo `models.py` tanki (polja, relacije, osnovne poizvedbe); poslovna pravila (DDV izračun, sorazmerni obračun, atomarno številčenje, Δ zaznava) živijo v `services.py` kot navaden Python, testabilen izolirano. Poslovna pravila morajo biti preverljiva neodvisno od tega, katero ogrodje jih kliče.

**Razvojna navodila za AI orodja** so v datoteki `AGENTS.md` v korenu repozitorija (odprt standard, ki ga bere ChatGPT/Codex in druga orodja — nadomešča prej predvideni CLAUDE.md, ki je vezan na Claude Code). Vsebina je v celoti v SAD §9.

---

## 14. Faznost

| Faza | Vsebina | Rezultat |
|---|---|---|
| **1** | Jedrni Django app-i (`companies`, `catalogs`, `contracts`, `invoicing`): modeli, domenske storitve, DDV, zaporedja, sorazmerni obračun, indeksacija, migracija in testi | Poslovna pravila delujejo brez vmesnika — dokazano s testi |
| **2** | `rendering`: PDF + eSLOG XML iz istega zapisa | Vzorčni PDF-ji za vseh 5 podjetij so pripravljeni za uporabnikov pregled v čarovniku |
| **3** | `ui` + mesečni postopek | Cel mesec izveden do koraka pred pošiljanjem |
| **4** | `delivery` + `archive` + `accounting_export`: e-pošta, preverjanje rezultatov pošiljanja, zbirno obvestilo uporabniku, kontrolni dokument, arhiv, kopije. **Vzporedno: pisanje navodil in operativnega priročnika (§16.3).** | Prvi resnični mesec |
| **5** | Ročne storitvene fakture + januarska indeksacija | Cel letni cikel pokrit |
| **6** | P1 funkcionalnosti | Nadzorna plošča, iskanje, filtri |

Faza 1 je najpomembnejša in najbolj mamljiva za preskok. Če poslovna pravila niso pravilna in pokrita s testi, bo vse ostalo videti, da deluje, in tiho izdajalo napačne račune.

---

## 15. Testni scenarij za predajo

Podrobnosti v ločenem dokumentu **Testni načrt**. Krovni scenarij:

Vzorčni mesec s 15 pogodbami, od tega ena z mešanim DDV, ena z indeksacijo, ena, ki poteče sredi meseca, ena nova.

1. Izračun → paketni pregled pokaže natanko pričakovane izjeme.
2. Popravek ene postavke → revizijski zapis, ponovna zahteva po potrditvi.
3. Potrditev → številke dodeljene, ena arhivirana kot neizdana z razlogom.
4. Pošiljanje z eno simulirano potrjeno napako → prizadeti korak je označen kot nezaključen, prikazani so račun, prejemnik in konkretna napaka; varen ponovni poskus pošlje isti PDF.
5. Simulirana prekinitev po začetku oddaje brez končne potrditve → račun dobi status `Rezultat ni znan – potrebno preverjanje` in se ne pošlje samodejno znova.
6. Preverjanje mape Poslano oziroma izrecna uporabnikova odločitev razreši neznani rezultat; dejanje je zapisano v revizijski sledi.
7. Po uspešni razrešitvi so vsi dokončani koraki označeni z zelenimi kljukicami, na uporabnikov e-naslov prispe zbirno poročilo in enak rezultat ostane viden na nadzorni plošči.
8. Simulirana napaka pošiljanja zbirnega poročila → statusi računov ostanejo nespremenjeni, v aplikaciji je prikazan razlog in omogočen ponovni poskus samo za poročilo.
9. Kontrolni dokument poslan; naknadni storno → opravilo se ponovno pojavi.
10. Obnova baze iz posnetka → stanje identično.

---

## 16. Kaj potrdi naročnik

| # | Predmet | Kdaj |
|---|---|---|
| 1 | Ta dokument (obseg in funkcije) | Pred začetkom razvoja |
| 2 | Vzorčni pravilen račun za vsako od 5 podjetij — potrdi uporabnik | Pred zaključkom čarovnika in produkcijsko izdajo |
| 3 | Logotipi, podpisi, IBAN-i, e-poštni predali, besedila sporočil | Pred fazo 2 |
| 4 | Seznam pogodb s parametri | Pred fazo 3 |
| 5 | **Razporeditev enot po DDV režimih — potrdi uporabnik** | **Pred zaključkom čarovnika in prvo resnično izdajo** |
| 6 | E-naslov računovodje in oblika kontrolnega dokumenta | Pred fazo 4 |
| 7 | Lokacije arhiva, iClouda in Dropboxa | Pred fazo 4 |
| 8 | En uporabnikov e-naslov za zbirna poročila o mesečnem postopku | Pred fazo 4 |

---

## 16.1 Navigacija in informacijska arhitektura

### Izhodišče

To ni aplikacija s štirinajstimi enakovrednimi funkcijami. **To je aplikacija z eno nalogo, ki se opravi dvanajstkrat letno, plus nekaj vzdrževanja.** Navigacija mora to odražati — sicer uporabnik vsak mesec išče isto stvar med enako pomembnimi postavkami.

| Kaj | Pogostost | Kje v navigaciji |
|---|---|---|
| Mesečni ciklus | 12× letno — **razlog za obstoj aplikacije** | Ni menijska postavka — **je nadzorna plošča** |
| Ročna storitvena faktura | 10–20× letno | Gumb na nadzorni plošči |
| Kontrolni dokument | 12× letno | Opravilo na nadzorni plošči |
| Najemne pogodbe | Nekajkrat letno | Prva raven menija |
| Arhiv in iskanje | Občasno | Prva raven menija |
| Podjetja, šifranti, podpisniki | Nekajkrat, nato skoraj nikoli | **Nastavitve** |

### Prva raven — štiri postavke

```
┌──────────────────────────────────────────────────────────┐
│  Računi        Nadzorna plošča · Pogodbe · Arhiv · ⚙     │
└──────────────────────────────────────────────────────────┘
```

**Nadzorna plošča** — vstopna točka in delovna površina. Opravila (mesečni paket, kontrolni dokument, potrditev DDV režimov, letni pregled pogodb), stanje po podjetjih, gumbi za ročno fakturo.

**Pogodbe** — seznam, filtri, vnos in urejanje, priloge.

**Arhiv** — izdani dokumenti, iskanje, filtriranje (P1).

**Nastavitve** — podjetja, podpisniki, šifranti DDV režimov in storitev, e-poštne nastavitve, en uporabnikov e-naslov za zbirna poročila, poti do arhiva in kopij ter prejemniki za računovodstvo.

Vse ostalo je znotraj teh štirih. Mesečni ciklus se **ne** pojavi v meniju: začne se z gumba na nadzorni plošči in ima svoj korakovnik.

### Drobtinice (breadcrumbs) — ne

Drobtinice se izplačajo pri treh ali več ravneh gnezdenja. Tu sta največ dve (Pogodbe → posamezna pogodba). Za orientacijo zadošča označena aktivna postavka menija in naslov strani.

**Obstaja pa drug, resničen problem orientacije: kje sem znotraj mesečnega ciklusa.** Šest korakov, od katerih je peti nepovraten. Rešitev ni drobtinica, ampak **korakovnik**, viden ves čas ciklusa:

```
① Mesec  ─  ② Kontrola  ─  ③ Izračun  ─  ④ Pregled  ─  ⑤ Potrditev  ─  ⑥ Zaključek
                                            ▲ trenutni korak
```

Uspešno opravljeni koraki imajo zeleno kljukico, prihodnji so zbledeli, trenutni je poudarjen. Korak z napako ima rdeče opozorilo in besedilo **»Ni zaključeno«**; pod korakovnikom je vedno naveden natančen prizadeti račun oziroma opravilo in razlog napake. Ker barva ni edini nosilec pomena, je stanje razumljivo tudi uporabniku, ki barv ne razlikuje.

---

### Načela oblikovanja

Pri interni aplikaciji za enega uporabnika **animacije, prelivi in vizualni učinki niso tisto, kar naredi izkušnjo dobro** — pogosto so v napoto. Kar jo naredi dobro:

| # | Načelo | Zakaj tu |
|---|---|---|
| 1 | **Hitrost** | Nič se ne zdi bolj dodelano kot orodje, ki se odzove takoj. Cilj C1 (pod 10 minut) je tudi oblikovalska zahteva. |
| 2 | **Nič presenečenj** | Aplikacija ne izgubi dela, ne vpraša dvakrat istega, ne zahteva osvežitve. Zaprt brskalnik sredi ciklusa in vrnitev pomeni isto mesto. |
| 3 | **Gostota, narejena pravilno** | Paketni pregled mora biti pregleden z enim pogledom: tabelarne številke (`tabular-nums`), zneski desno poravnani, decimalke navpično poravnane. |
| 4 | **Stolpec Δ je podpis izdelka** | Celotna vrednost aplikacije je, da naredi spremembo vidno. Oblikovalski trud gre sem, ne v okrasje drugod. |
| 5 | **Delo brez miške** | Premik po vrsticah s tipkovnico, presledek za potrditev, Enter naprej. Uporabnik, ki to dela vsak mesec, bo to hotel. |
| 6 | **Zadržanost pri barvi** | Ena poudarna barva, samo za dejanja in izjeme. Izjeme označene tudi z debelino ali okvirjem, ne le z barvo — zaradi tiskanja in berljivosti. |
| 7 | **Prazni zasloni učijo** | Prvi zagon nima podatkov; zaslon naj pove, kaj narediti. |
| 8 | **Potrditve namesto modalnih oken** | Sporočilo o tem, kaj se je zgodilo, ne okno, ki ga uporabnik zapre brez branja. |

**Vizualna smer:** umirjeno, gosto, tipografsko orodje — bližje profesionalnemu finančnemu vmesniku kot spletni aplikaciji. Skoraj monokromatsko, ena poudarna barva, brez zaobljenih kartic in senc. Poudarek na berljivosti številk in na tem, da je cel mesec viden na enem zaslonu brez drsenja.

**Postopek potrditve vizualne smeri:** po potrditvi žičnih modelov se izdela **en zaslon v visoki ločljivosti — paketni pregled (07)** — z dejanskimi barvami, tipografijo in gostoto. En zaslon, ker nosi celoten izdelek in ker je najcenejši način za dogovor o smeri, preden se ta razlije čez vse zaslone.

---

## 16.2 Pomoč pri vnosu — konvencija za polja

Vsako polje mora biti razumljivo brez ugibanja, a hkrati brez šuma za uporabnika, ki aplikacijo uporablja mesečno in polja pozna. Zato **stopnjevana pomoč glede na pogostost uporabe**, ne enotna za vsa polja.

### Lestvica sredstev, od najcenejšega navzgor

| Sredstvo | Kdaj uporabiti | Omejitve |
|---|---|---|
| **1. Boljša oznaka polja** | Vedno prva poteza | — |
| **2. Trajno pojasnilo pod poljem** | Podatek, ki ga uporabnik potrebuje **vsakič** | Zavzame prostor; po tretji uporabi postane šum, če ni res potreben |
| **3. Namig v polju (placeholder)** | **Samo primer oblike**, ko oznaka že pove, kaj polje je | Izgine ob tipkanju, slaba berljivost, nezanesljivo za bralnike zaslona — **nikoli ne sme nositi bistvene informacije** |
| **4. Ikona "i" z razlago ob prehodu miške** | **Samo dopolnilna** razlaga, ki je izkušen uporabnik ne potrebuje | Skrito privzeto; ne deluje na dotik; pogosto spregledano |
| **5. Sprotna validacija s konkretnim sporočilom** | Zadnja obramba, ko je vnos vseeno napačen | Nastopi šele po napaki |

**Vodilno pravilo: bistvena informacija se nikoli ne skriva za prehodom miške.** Če uporabnik potrebuje podatek, da polje sploh izpolni, mora biti viden brez interakcije.

### Uporaba v tej aplikaciji

**Popravek oznak, ki so bile dvoumne:**

| Prej | Bolje |
|---|---|
| Oznaka pogodbe | **Številka pogodbe** — namig v polju: `npr. 12/2024` — pojasnilo: *Iz podpisanega izvoda pogodbe. Izpiše se na računu kot sklic.* |
| Veljavnost | **Trajanje pogodbe** (§6.10) |
| Rekapitulacijska skupina | **Skupina za rekapitulacijo DDV** — pojasnilo: *Postavke z isto skupino se na računu seštejejo v eno vrstico.* |

**Stopnjevanje po zaslonih:**

| Zaslon | Pogostost uporabe | Raven pomoči |
|---|---|---|
| Mesečni ciklus (06–10) | Vsak mesec | **Minimalna.** Uporabnik postopek obvlada po drugem mesecu; pojasnila bi bila šum. |
| Ročna faktura (11) | Občasno | Namig v polju za znesek: *vpiši znesek z DDV*. |
| Pogodbe, podjetja (02, 04, 05) | Nekajkrat letno | **Trajno pojasnilo** pri poljih, ki se redko izpolnjujejo. |
| Šifrant DDV (03) | Redko, visoke posledice | **Trajno pojasnilo + povezava na navodila.** Napačen DDV režim je najdražja napaka v sistemu. |

**Polja, ki brez pojasnila ne smejo ostati** (redka uporaba, visoke posledice): DDV režim in referenca izjave, tip trajanja in samodejno podaljšanje, pogoj indeksacije, dostavni kanal, rekapitulacijska skupina.

**Prazni zasloni so navodilo.** Ob prvem zagonu ni podjetij, pogodb ne šifrantov. Prazen zaslon naj pove, kaj narediti, ne le da je prazen: *"Ni še nobene najemne pogodbe. Najprej dodajte podjetje in nepremičnino, nato pogodbo."*

### Tehnična opomba

V Djangu se pojasnilo definira kot `help_text` na polju modela in se samodejno izpiše v adminu **in** v uporabniških obrazcih. Pojasnilo tako živi ob definiciji polja, ne podvojeno v predlogah — sprememba na enem mestu velja povsod.

---

## 16.3 Predaja: uporabniška navodila in operativni priročnik

Navodila se pišejo **med fazama 3 in 4, ne na koncu.** Pisanje navodil o programski opremi, katere podrobnosti si medtem pozabil, je počasnejše in slabše. Poleg tega so scenariji iz Testnega načrta (§15) v veliki meri že vsebina navodil — isti tokovi, drug namen.

Trije dokumenti, vsak z drugačno vlogo:

### 1. Pomoč v aplikaciji (§16.2)

Pokrije večino vsakodnevnih vprašanj. **Najpomembnejši od treh**, ker je edini, ki ga uporabnik dejansko vidi v trenutku, ko ima vprašanje.

### 2. Navodila za uporabo — 8–12 strani

**Urejena po opravilih, ne po funkcijah.** Ne "Zaslon Pogodbe", ampak "Kako dodam novega najemnika". Vsebina:

- Mesečni postopek od začetka do konca (s posnetki zaslonov)
- Nova pogodba, nov najemnik
- Ročna storitvena faktura
- Januarska indeksacija in letni pregled pogodb
- Storno in dobropis
- Kontrolni dokument za računovodjo
- Kaj pomeni posamezni DDV režim (povezano iz šifranta)

**Posnetek zaslona namesto opisa, kjer je mogoče.** Deset minut posnetega videa mesečnega postopka je za enega uporabnika vredno več kot dvajset strani besedila — priporočeno kot dodatek, ne nadomestilo.

### 3. Operativni priročnik — 4–6 strani

**Dokument, ki ga nihče ne bere, dokler ga nujno ne potrebuje.** Zato mora biti ločen, kratek in natančen. Vsebina:

| Postopek | Zakaj tukaj |
|---|---|
| **Obnova iz varnostne kopije** | Postopek po korakih, s preverjanjem, da je obnova uspela |
| Kaj narediti, če pošiljanje ne uspe | Razlaga statusov, varen ponovni poskus |
| Kaj narediti, če se številčenje pokvari | Najbolj tvegan ročni poseg — mora biti opisan, ne improviziran |
| Prehod v novo leto | Kaj se resetira (`ST`) in kaj ne (`NP`); kaj preveriti |
| Selitev na nov računalnik | Kaj prenesti, kaj ponovno nastaviti (Keychain, poti do iCloud in Dropbox) |
| Prva namestitev in nastavitev | Za primer ponovne postavitve |

**Razlog za ločitev od navodil za uporabo:** vsakodnevnih postopkov se uporabnik nauči z uporabo. Postopka obnove iz kopije se ne nauči nikoli, dokler ga ne potrebuje — in takrat je pod pritiskom. Ta dokument mora biti berljiv v takem trenutku.

### Zahteva za predajo

Predaja aplikacije ni zaključena, dokler ni **obnova iz varnostne kopije dejansko izvedena po zapisanem postopku** (§11.3, P0-31) — kot preizkus, da je priročnik pravilen, ne le da obstaja.

---

## 16.4 Prvi zagon in začetna nastavitev

Ob prvem zagonu aplikacija uporabnika vodi skozi razumljiv čarovnik. Začetni sklop ni zaključen pri vnosu »stranke«, temveč vključuje celotno verigo podatkov, potrebno za pripravo računa:

1. podjetje;
2. nepremičnina;
3. enota;
4. najemnik;
5. pogodba;
6. pogodbene postavke;
7. številčenje računov, lokacije shranjevanja, logotip, podpis, bančni in drugi obvezni podatki podjetja.

Izraz **»stranka«** se lahko v uporabniškem vmesniku uporablja kot uporabniku razumljiv skupni izraz, podatkovni model pa mora ohraniti ločene zapise za najemnika, nepremičnino, enoto, pogodbo in pogodbene postavke. Začetna nastavitev posamezne pogodbe je uspešna, ko so vneseni in preverjeni vsi obvezni podatki za izdelavo vzorčnega računa.

### Preverjanje lokacij shranjevanja

Pred zaključkom čarovnika morajo biti konfigurirane in uspešno preverjene vse tri zahtevane lokacije: lokalni arhiv, iCloud arhiv in Dropbox mapa za varnostne posnetke. Aplikacija za vsako lokacijo izvede neuničujoč preizkus: zapiše začasno datoteko, jo ponovno prebere ter jo po uspešnem preverjanju odstrani.

Če katerakoli lokacija preizkusa ne prestane, začetne nastavitve ni mogoče zaključiti in produkcijska izdaja ostane blokirana. Aplikacija uporabniku pokaže, katera lokacija ni prestala preverjanja in zakaj.

Ta pogoj velja za pravilnost začetne konfiguracije. Kasnejša začasna nedosegljivost iClouda se obravnava po §11.2: pošiljanje računa ni blokirano, račun pa ostane v stanju `Arhiviranje v teku`. Ta odločitev ne določa dodatnega blokirnega pravila za kasnejšo nedosegljivost Dropboxa.

### Vzorčni račun

Pred zaključkom čarovnika mora aplikacija za **vsako od petih podjetij** ustvariti in prikazati vzorčni račun iz ene izbrane veljavne pogodbe tega podjetja. Vzorec uporablja dejansko predvideno PDF-predlogo ter prikaže logotip, podpis, podatke podjetja in najemnika, pogodbene postavke, DDV, plačilni rok in bančne podatke.

Vzorčni račun:

- ni izdan račun;
- nima uradne številke računa in je ne porabi;
- se ne pošlje po e-pošti;
- se ne shrani v uradni arhiv računov;
- je jasno označen z **»VZOREC – NI RAČUN«**;
- ne vpliva na številčna zaporedja ali evidence izdanih računov.

Uporabnik izrecno potrdi vzorec za vsako podjetje. V podatkih se ločeno zabeležijo podjetje, čas uporabnikove potrditve in različica uporabljene PDF-predloge; vzorec se ne evidentira kot izdani dokument. Čarovnik se lahko zaključi in produkcijska izdaja omogoči šele, ko so izpolnjeni drugi obvezni pogoji ter ima vseh pet podjetij veljavno uporabnikovo potrditev vzorca.

### Ločeni potrditvi in zastarelost

`ddv_razporeditev_potrjena` pomeni **uporabnikovo potrditev DDV-nastavitev za posamezno podjetje**, ne potrditve računovodje. Potrditev DDV-nastavitev in potrditev celotnega vzorca sta ločena poslovna predpogoja in se shranita ločeno, čeprav ju lahko uporabnik opravi na istem koraku čarovnika.

- Sprememba DDV-nastavitev razveljavi potrditev DDV in potrditev vzorca.
- Sprememba računovodsko pomembnih podatkov, načina izračuna, DDV-ja, obveznih sestavin računa ali PDF-predloge razveljavi potrditev vzorca.
- Manjša vizualna sprememba, ki ne vpliva na vsebino računa, ne razveljavi potrditve DDV.
- Sprememba podatkov podjetja, logotipa, podpisa, bančnih podatkov ali predloge mora najmanj sprožiti opozorilo, da je priporočljiv ponovni pregled vzorca; če je sprememba računovodsko pomembna ali spremeni PDF-predlogo, je potrditev vzorca zastarela.

### Vzdrževanje, arhiviranje in izbris začetnih podatkov

Podjetja, nepremičnine, enote, najemnike, pogodbe in pogodbene postavke je po začetni nastavitvi mogoče dodajati in urejati. Fizični izbris je dovoljen samo, če zapis **še nikoli ni bil uporabljen** in nima nobene poslovne, podatkovne ali podrejene reference.

Fizični izbris ni dovoljen, če je zapis povezan z:

- osnutkom ali izdanim računom;
- pogodbo ali pogodbeno postavko;
- nepremičnino, enoto ali najemnikom;
- PDF-jem, prilogo ali drugim ustvarjenim dokumentom;
- številčnim zaporedjem;
- pošiljanjem ali evidenco pošiljanja;
- uporabnikovo potrditvijo vzorca ali DDV-nastavitev;
- drugo poslovno pomembno zgodovino.

Če ima zapis podrejene zapise, fizični izbris ni dovoljen, dokler obstaja katerakoli povezava. Vsak podrejeni zapis mora tudi sam izpolnjevati pogoje za fizični izbris; kaskadni fizični izbris poslovnih zapisov ni dovoljen.

Zapis, ki ga ni dovoljeno fizično izbrisati, se lahko samo arhivira oziroma mehko izbriše. Arhivirani zapis ostane v zgodovini, ni ponujen pri pripravi novih računov in ga je mogoče ponovno aktivirati, kadar to dovoljujejo njegova veljavnost, referenčna celovitost in druga poslovna pravila.

Če fizični izbris ni dovoljen, aplikacija uporabniku jasno prikaže konkretne povezave oziroma kategorije zgodovine, ki izbris preprečujejo, in ponudi arhiviranje. Pred dovoljenim fizičnim izbrisom prikaže jasno opozorilo in zahteva izrecno uporabnikovo potrditev.

Po fizičnem izbrisu v revizijski zgodovini ostanejo najmanj vrsta izbrisanega zapisa, čas izbrisa in dejanje uporabnika. Poslovni zapis sam ne ostane aktivna entiteta, revizijski zapis pa ne sme hraniti njegove celotne poslovne vsebine.

### Vloga računovodje

Računovodja ni udeleženec ali potrjevalec čarovnika, nima dostopa do aplikacije in ne vpliva na zaključek čarovnika, dovoljenje za produkcijsko izdajo, status računa ali uspešnost mesečnega postopka. Njegovi obstoječi operativni vlogi ostajata omejeni na možnost prejema kopije BCC in kontrolnega dokumenta; sam prejem ne pomeni potrditve.

Podrobna stanja čarovnika, nadaljevanje po prekinitvi, natančna podatkovna struktura potrditev ter preslikava zgornjih pravil na posamezne modele bodo opredeljeni v prihodnjem FSD za prvi zagon. Ta FSD mora ohraniti zgornje poslovne omejitve, zaščito pred kaskadnim izbrisom, nespremenljivost izdanih računov in revizijsko sled fizičnih izbrisov.

---

## 17. Odprta vprašanja
Nobeno ne blokira začetka razvoja.

**Odločeno v v1.1 (prejšnje O2):** v imenih datotek se uporabi polni naziv podjetja, počiščen za datotečni sistem: brez šumnikov in pik, presledki se zamenjajo z vezaji. Kratica ali dodatna šifra se ne uvajata.

| # | Vprašanje | Kdo | Kdaj |
|---|---|---|---|
| O1 | Ali naj se računovodji poleg CSV pošlje tudi PDF izvod kontrolnega dokumenta? | Uporabnik | Pred fazo 4 |
| O3 | Ali naj bo rok za kontrolni dokument prilagojen mesečnemu ali trimesečnemu poročanju DDV? | Uporabnik | Pred fazo 4 |
| O4 | Ali je treba podpreti obstoječi format za uvoz v računovodski program? | Uporabnik | Pred fazo 4 |

---

## Dodatek A — Privzeti šifrant DDV režimov

Ob namestitvi. Uporabnik lahko vrstice ureja, dodaja in deaktivira.

| Šifra | Naziv | Način | Stopnja | Rekap. skupina | Zahteva ref. |
|---|---|---|---|---|---|
| `OBDAVCENO_45` | Najem z izjavo po 45. členu | `OBDAVCENO` | 22 % | `OBD_22` | **da** |
| `OBDAVCENO_STANDARD` | Obdavčeno po splošni stopnji | `OBDAVCENO` | 22 % | `OBD_22` | ne |
| `OBDAVCENO_ZNIZANA` | Obdavčeno po nižji stopnji | `OBDAVCENO` | 9,5 % | `OBD_95` | ne |
| `OPROSCENO_44_2` | Najem nepremičnin — oproščeno | `OPROSCENO` | 0 % | `OPR_BREZ_ODB` | ne |
| `OPROSCENO_46` | Dobava v EU — oproščeno s pravico do odbitka | `OPROSCENO` | 0 % | `OPR_S_ODB` | ne |
| `NI_PREDMET` | Ni predmet DDV | `NI_PREDMET` | 0 % | `NI_PREDMET` | ne |
| `OBRNJENA_76A` | Prenos davčne obveznosti | `OBRNJENA_DAVCNA` | 0 % | `OBRNJENA` | ne |

Klavzule se vnesejo ob namestitvi in jih pred produkcijsko uporabo potrdi uporabnik.

---

## Dodatek B — Privzeta razporeditev enot po režimih

Predlog, ki ga uporabnik popravi in potrdi v vmesniku.

| Vrsta enote | Privzeti režim |
|---|---|
| Poslovni prostor, pisarna, lokal, skladišče | `OBDAVCENO_45` |
| Parkirno mesto, garaža | `OBDAVCENO_STANDARD` |
| Trajno instalirana oprema | `OBDAVCENO_STANDARD` |
| Poslovni prostor — najemnik brez izjave | `OPROSCENO_44_2` |
