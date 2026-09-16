# Ocena obsega in vrednosti

**Projekt:** Aplikacija za izdajanje najemniških računov
**Različica:** 1.1 · **Podlaga:** PRD v1.1 in FSD faze 1 v1.1, 25. avgust 2026

**Spremembe v1.1:** usklajena terminologija s potrjenim skladom Django/HTMX/WeasyPrint in migracijskim mehanizmom iz FSD. Ocene ur, vrednosti in skupnega trajanja ostajajo nespremenjene.
**Namen:** ocena vrednosti in trajanja — ni ponudba in ni zavezujoč obseg

> **Zanesljivost ocene: srednja za ročno kodiranje, nizka do srednja za vibe coding (glej §4.5).** Ocene programske opreme so pri tem obsegu sistematično prenizke. Razponi so široki namenoma. Vsaka številka je informirana špekulacija, ne izračun.

---

## 1. Dve različni številki za dva različna namena

| Vprašanje | Kaj šteje |
|---|---|
| **Koliko je aplikacija vredna?** | Ure izkušenega razvijalca × tržna urna postavka |
| **Kdaj bo gotova?** | Ure študenta začetnika × njegova razpoložljivost |

Ti dve številki se razlikujeta za faktor dva do tri. Meša se ju pogosto in vodi v napačna pričakovanja glede roka.

---

## 2. Obseg dela po fazah — izkušen razvijalec

| Faza | Vsebina | Ur (nizko) | Ur (visoko) |
|---|---|---|---|
| **1** | Jedrni Django app-i: modeli in domenske storitve za pogodbe, šifrante, DDV izračun, zaporedja, sorazmerni obračun, indeksacijo, migracijo in teste | 80 | 120 |
| **2a** | `rendering`: WeasyPrint predloga, logotip, podpis, več strani, mešana rekapitulacija | 40 | 60 |
| **2b** | eSLOG 2.0: preslikava, generiranje, XSD validacija | 40 | 80 |
| **3** | `ui`: Django templates + HTMX, mesečni ciklus, paketni pregled, izjeme, vzdrževanje podjetij/najemnikov/pogodb/šifrantov | 100 | 150 |
| **4** | `delivery` + `archive` + `accounting_export`: e-pošta, Keychain, čakalna vrsta, dvotirni arhiv, Dropbox posnetki, obnova, kontrolni dokument | 60 | 90 |
| **5** | Ročne storitvene fakture, januarska indeksacija, storno/dobropis | 30 | 45 |
| **5a** | Trajanje in prenehanje pogodb, samodejno podaljšanje, sklici na računu, priloge k pogodbam | 25 | 45 |
| **6** | P1: nadzorna plošča, iskanje, filtri | 40 | 60 |
| **—** | Postavitev okolja, lokalni zagon na macOS in popravki po testiranju | 30 | 45 |
| **—** | Uporabniška navodila + operativni priročnik (§16.3 PRD) | 20 | 35 |
| | **SKUPAJ** | **475** | **750** |

Delovna predpostavka: **~600 ur** izkušenega razvijalca.

---

## 3. Tržna vrednost

| Model | Urna postavka (SI, 2026) | Vrednost pri 600 h |
|---|---|---|
| Agencija za razvoj programske opreme | 55–80 € | **33.000 – 48.000 €** |
| Samostojni razvijalec s področno izkušnjo | 35–50 € | **21.000 – 30.000 €** |
| Študent / začetnik (tržno neprimerljivo) | — | — |

**Realen razpon za naročilo pri zunanjem izvajalcu: 22.000 – 45.000 €.**

Agencija bi verjetno ponudila na zgornji polovici razpona zaradi davčne odgovornosti in zahteve po skladnosti z ZIERDED.

---

## 4. Trajanje pri študentu začetniku

### 4.1 Dve različni poti do te ocene

Prejšnja tabela (ur × 2–3) je ocena za **ročno pisanje kode** — kot da AI ne obstaja. Vprašal si po vibe codingu, kar je druga pot z drugačno krivuljo pospeška po fazah. Množitelja se ne da preprosto zamenjati z manjšim; treba je pogledati, **kje** AI dejansko pospeši in kje ne.

### 4.2 Kje AI pospeši in kje ne — po fazah

| Faza | Ročno (izkušen razv.) | Kaj AI pospeši | Kaj AI ne pospeši | Pospešek vibe coding |
|---|---|---|---|---|
| **1** jedrni Django app-i | 80–120 h | Boilerplate entitet, CRUD, migracije, testni ogrodji | **Poslovna pravila** — zaokroževanje, DDV logika, sorazmerni obračun, atomarnost. To je mišljenje, ne tipkanje; tu se AI enako pogosto moti kot pomaga. | 1,3–1,8× |
| **2a** PDF renderer (WeasyPrint) | 40–60 h | HTML/CSS predloga in ponavljajoče se komponente | Vizualno fino usklajevanje tiskovne postavitve, prelomi strani, pisave in mešana rekapitulacija | 1,5–2× |
| **2b** eSLOG *(izven MVP, glej §5)* | 40–80 h | Skeletna koda za XML | XSD shema, davčne kategorije — natančnostno delo, kjer napaka ni vidna dokler ne poči pri validaciji | 1,2–1,5× |
| **3** `ui` (Django templates + HTMX) | 100–150 h | Osnovni zasloni, obrazci, tabele in standardni CRUD vzorci | **Večstopenjski mesečni proces.** Ohranjanje stanja, Δ pregled, izjeme in varno nadaljevanje po prekinitvi zahtevajo razmislek in testiranje, ne le izdelavo obrazcev. | 1,2–1,6× |
| **4** `delivery` + `archive` | 60–90 h | SMTP, čakalna vrsta, retry vzorci (blok 32 iz Vibe Engineering) in uporaba knjižnice `keyring` so dobro pokriti | macOS poti, iCloud/Dropbox vedenje in dejansko testiranje obnove iz kopije | 1,4–1,8× |
| **5** Ročne fakture, indeksacija, storno | 30–45 h | Standardni CRUD vzorci | Malo — to je večinoma "več istega" iz faze 1 | 1,6–2× |
| **6** P1 (izven MVP) | 40–60 h | Dashboard, filtri, iskanje — zelo dobro pokrito | — | 1,8–2,2× |
| Okolje in lokalni zagon | 40–65 h | Skripte, dokumentacija in standardna postavitev `venv` | Dejanski preizkus zagona na macOS, dovoljenja, poti, `launchd` in selitev na drug računalnik so operativno delo — čas ostane | 1,1–1,3× |

**Ključna ugotovitev:** pospešek ni enakomeren. Najvišji je tam, kjer je najmanj tveganja (P1, ogrodje), najnižji tam, kjer je največ tveganja (poslovna pravila, binding, natančnost). To je pomembno, ker pomeni, da vibe coding **ne skrajša kritične poti sorazmerno s celotnim projektom** — skrajša predvsem odpadni čas okoli nje.

### 4.3 Kaj vibe coding doda, česar ročno kodiranje nima

Ne gre le za pisanje kode. Doda se:

- **Pregled generiranega,** ker junior brez izkušnje težje loči pravilno od prepričljivo napačnega — posebej pri davčni logiki, kjer napaka izgleda kot delujoča koda.
- **Popravljanje halucinacij** pri podrobnostih WeasyPrinta, HTMX-a, macOS integracij in eSLOG sheme — AI lahko predlaga API ali vedenje, ki v uporabljeni različici ne obstaja.
- **Učenje branja generirane kode**, kar je za začetnika počasnejše kot pisanje lastne, dokler se ne navadi.
- **Prihranek** pri vsem ponavljajočem — CRUD, obrazci, migracije, testno ogrodje, dokumentacija.

Neto učinek teh štirih dejavnikov je razlog, zakaj spodnji množitelj ni preprosto "polovica ročnega časa".

### 4.4 Skupna ocena — vibe coding

| Obseg | Ročno (izkušen, iz §2) | Množitelj junior + AI | Ur skupaj |
|---|---|---|---|
| **Polni obseg** (vse faze) | 475–750 h | **1,4–1,8×** | **665 – 1.350 h** |
| **MVP** (brez eSLOG in P1, §5) | 340–470 h | **1,4–1,8×** | **475 – 845 h** |

Za primerjavo: ročno kodiranje brez AI je pri istem izvajalcu doseglo 1.200–1.850 h (polni obseg) po množitelju 2–3×. **Vibe coding torej realno skrajša trajanje na približno 55–70 % ročnega pristopa**, ne na tretjino ali polovico, kot se pogosto pričakuje.

| Razpoložljivost | MVP (475–845 h) | Polni obseg (665–1.350 h) |
|---|---|---|
| 10 ur / teden | **11 – 19 mesecev** | **15 – 31 mesecev** |
| 20 ur / teden | **6 – 10 mesecev** | **8 – 16 mesecev** |
| 40 ur / teden (poletje) | **3 – 5 mesecev** | **4 – 8,5 mesecev** |

### 4.5 Zakaj ni bolj optimistično

Splošno prepričanje je, da AI skrajša razvoj za 5–10×. To drži za **skripte, prototipe in znane vzorce** — ne za projekt s tremi lastnostmi, ki jih ta ima hkrati:

1. **Davčna pravila z ničelno toleranco za tiho napako** (§6.2–6.3 PRD) — tu je pregled obvezen ne glede na to, kdo ali kaj je kodo napisalo.
2. **Proces s stanji in nepovratnimi koraki** — mesečni paket, številčenje, Δ izjeme in nadaljevanje po prekinitvi zahtevajo preverjeno poslovno logiko, tudi če je Django dobro zastopan.
3. **Junior brez izkušnje presojati generirano** — največji posamični dejavnik. Izkušen razvijalec z AI doseže bistveno višji pospešek, ker prepozna napako v treh sekundah namesto v treh urah razhroščevanja.

Tretja točka je edina, ki se s časom sama popravi — vrednost tega projekta ni le aplikacija, ampak tudi razvijalec, ki bo čez leto dni bistveno hitrejši od te ocene.

**Zanesljivost te ocene: nizka do srednja.** Za razliko od ur ročnega kodiranja (kjer obstajajo desetletja industrijskih podatkov), za pospešek vibe codinga pri juniorju na tem specifičnem skladu ni zanesljivih referenčnih podatkov — gre za informirano oceno na podlagi znanih trendov (učna zastopanost knjižnic, narava dela po fazah), ne izmerjeno statistiko. Priporočam meriti dejansko hitrost po prvih 2–3 tednih faze 1 in oceno takrat umeriti.

---

## 5. Priporočilo za skrajšanje: MVP v1

Predlagana ožja prva različica, ki v celoti nadomesti Excel:

| Vključeno | Izpuščeno iz v1 |
|---|---|
| Faza 1 v polnem obsegu | **eSLOG generiranje (2b)** — 40–80 h, zahteva ga nihče |
| PDF renderer (2a) | Nadzorna plošča in iskanje (6) |
| Mesečni ciklus in paketni pregled | Januarska indeksacija (prvo leto ročno) |
| Pošiljanje, arhiv, kopije | |
| Kontrolni dokument za računovodjo | |
| Ročne storitvene fakture | |

**MVP obseg: ~340–470 ur izkušenega razvijalca**, torej 475–845 ur študenta pri vibe codingu.

### Zakaj prav eSLOG ven

Je najdražja posamična postavka, ki danes ne prinese ničesar — noben najemnik XML ne zahteva, obveznost pa nastopi **1. januarja 2028**. Časa je dovolj.

**Ključno:** iz PRD ostane arhitekturna zahteva, da je račun **strukturiran zapis, iz katerega se PDF renderira** (§10 PRD). Ta odločitev je brezplačna in se sprejme zdaj. Generiranje XML se doda pozneje kot drugi renderer, brez predelave modela.

Če se to zamenja — če se račun modelira kot "PDF z vsebino" — je poznejša dograditev nekajkrat dražja od prihranka.

---

## 6. Vrednost za naročnika — poštena primerjava

| Postavka | Ocena |
|---|---|
| Prihranek časa: ~3 h/mesec × 12 × 5 let = 180 h | Odvisno od vrednotenja ure vodstvenega časa |
| Odpravljene napake (napačna številka, izpuščen račun, napačen DDV) | Nizka verjetnost, visoka posamična cena |
| Alternativa SaaS (Minimax/Saop, ~30–50 €/mesec za več podjetij) | **1.800 – 3.000 € / 5 let** |
| Lastna aplikacija pri zunanjem izvajalcu | **22.000 – 45.000 €** |

**Poštena ugotovitev:** na čistem izračunu donosnosti se lastna aplikacija pri zunanjem izvajalcu ne izide. SaaS je za ta obseg desetkrat cenejši.

Upravičenost lastne rešitve stoji na drugih temeljih, ki so bili navedeni in so legitimni:

1. **Delovni tok, prilagojen dejanskemu postopku** — nič, česar se ne uporablja.
2. **Podatkovna suverenost** — vsi podatki lokalno.
3. **Usposabljanje študenta** — resničen, merljiv izid, ki ga SaaS ne prinese.
4. **Lastništvo rešitve** — brez odvisnosti od dobavitelja in njegovih sprememb cen.

Pri interni izvedbi je denarni strošek predvsem **strošek študentovega časa**, ne 30.000 €. Zato izračun donosnosti ni pravi okvir odločanja — je pa pravi okvir za odgovor na vprašanje "koliko je to vredno", če bi jo kdaj prodajali ali dali oceniti.

---

## 7. Kaj ocena predpostavlja

- Obseg je zamejen s PRD v1.1 in FSD faze 1 v1.1 ter se med izvedbo ne širi.
- En uporabnik, ena naprava, samo lokalni strežnik na `127.0.0.1`, brez javnega gostovanja.
- Ni polnega uvoza zgodovinskih računov iz Excela. Vključeni so začetni vnos pogodb, nastavitev zadnjih številk zaporedij, izhodiščni podatki indeksacije in tehnični referenčni zapisi za Δ primerjavo, kot določa FSD §7.
- Računovodja odgovori pravočasno; potrditev DDV režimov ni na kritični poti razvoja.
- Za eSLOG (če se izvede) je na voljo shema in vsaj en primer, ki ga prejemnik dejansko sprejme.
- Ni zahtev po skladnosti, ki bi presegale ZDDV-1 in ZIERDED.

**Kar bi oceno podrlo navzgor:** zahteva po več uporabnikih, spremljanju plačil, obračunu obratovalnih stroškov, ali po mobilni različici. Vsaka od teh je nov projekt, ne dodatek.

---

## 8. Predlagani naslednji koraki

| # | Dokument | Namen | Obseg |
|---|---|---|---|
| ✓ | **SAD v1.1** — arhitekturni načrt | Skelet projekta: struktura, plasti, konvencije, vzorci | dostavljeno |
| ✓ | **UI/UX wireframi in tokovi** | Razporeditev zaslonov, poimenovanja, poti uporabnika — pred kodiranjem vmesnika | dostavljeno |
| ✓ | **FSD za fazo 1 v1.1 FINAL** | Podroben vhod za razvoj: entitete, pravila, robni primeri, migracija | dostavljeno |
| 1 | **Testni načrt za fazo 1** | Scenariji in kriteriji sprejemljivosti na podlagi FSD v1.1 | naslednji korak |
| 2 | FSD in testi za faze 2–6 | Tik pred vsako fazo, ne vnaprej | po fazah |

Pisanje polne funkcionalne specifikacije za vse faze vnaprej je odpadek: model podatkov se ob prvem stiku z resničnostjo spremeni in polovico bi bilo treba prepisati.
