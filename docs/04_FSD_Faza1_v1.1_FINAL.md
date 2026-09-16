# Funkcionalna specifikacija — Faza 1 (v1.1 FINAL)

**Aplikacija Marjan — najemniško zaračunavanje**
Interni dokument za razvojno ekipo (AI-podprt razvoj, ChatGPT/Codex). Ni za naročnika.

Obseg te specifikacije: `/domain` + `/data` za module `companies`, `catalogs`, `contracts`, `invoicing` (jedro). Cilj faze 1 po SAD-u: poslovna pravila delujejo in so dokazana s testi, brez zagnanega vmesnika. `/render`, `/ui`, `/delivery`, `MonthlyBatch` kot orkestracijski model in priloge k pogodbi so izven obsega tega dokumenta (faze 2–5).

**Ta različica (v1.1) je dokončna za fazo 1.** Nastala je skozi dve razpravni fazi: (a) pet vsebinskih sklopov (podatkovni model, pravila izračuna, robni primeri, validacija partnerjev, indeksacija) in (b) končni pregled vseh štirih izvornih dokumentov (PRD, SAD, agents.md, UIUX prototipi) proti temu dokumentu, ki je odkril tri dodatne vrzeli in dve neskladji med samimi izvornimi dokumenti. Priloga B beleži vseh 19 dopolnil kronološko. Priloga C beleži popravke, potrebne v drugih dokumentih v1.0 — to poglavje je namenjeno neposrednemu prenosu v PRD/SAD/agents.md.

---

## 1. Podatkovni model

### 1.1 `companies`

**Zakaj ta app obstaja ločeno.** Pet podjetij ni pet uporabnikov aplikacije — je pet izdajateljev na istih računih, z ločenimi zaporedji, podpisniki in poslovnimi podatki. Ločitev v svoj Django app pomeni, da lahko šesto podjetje čez leta dodaš brez posega v `contracts` ali `invoicing`.

**Company**

| Polje | Tip | Omejitve / opombe |
|---|---|---|
| `naziv` | CharField | required |
| `davcna_stevilka` | CharField | required, unique med aktivnimi; validacija modul 11, glej §3 |
| `maticna_stevilka` | CharField | required, natanko 10 mest (potrjeno — glej Prilogo B, dopolnilo 18) |
| `naslov` | CharField | required |
| `trr` | CharField | required — IBAN za plačilo na računu |
| `logotip` | FileField | required pred fazo 2 (PRD P0-1, §16 postavka 3); tip preverjen po vsebini datoteke (dopolnilo 18) |
| `ddv_zavezanec` | BooleanField | default `True` |
| `email_racunovodja` | EmailField | required — prejemnik kontrolnega dokumenta (faza 4) |
| `email_posiljatelj` | EmailField | required |
| `keychain_kljuc_posiljatelja` | CharField | required — referenca v macOS Keychain (`keyring`), NIKOLI geslo v bazi |
| `privzeti_dostavni_kanal` | CharField, choices: `PDF`/`ESLOG`/`OBOJE` | default `PDF` |
| `rok_placila_dni_privzeto` | IntegerField | default (npr. 15) |
| `ddv_razporeditev_potrjena` | BooleanField | default `False` — enkratna potrditev razporeditve enot po DDV režimih, PRD §16 postavka 5, P0-37 (dopolnilo 19) |
| `aktivno` | BooleanField | default `True` |

*Zakaj `ddv_razporeditev_potrjena` na `Company`, ne kot ločen model.* PRD (P0-37, §16 postavka 5) zahteva enkratno potrditev "pri računovodji", preden je dovoljena prva resnična izdaja — to je gate, ne proces s koraki, zato zadostuje en boolean na podjetju. Uveljavlja se v §2.5 (Predpogoji podjetja), ne v adminu.

*Zakaj `Company` nima polja `kratica`.* PRD-jevo odprto vprašanje O2 ("polni naziv, kratica ali šifra v imenu datoteke?") je zdaj rešeno — poln naziv, počiščen za datotečni sistem (potrjeno). Ločeno polje za kratico ni več potrebno; ime datoteke se izpelje iz `naziv` prek `core.pravila.slugify_naziv()` (§1.5). Glej Prilogo B, dopolnilo 20.

**Signatory**

*Zakaj ločeno od `Company`, ne kot polje.* Podpisnik se menja (zaposlitev, upokojitev), pogodba pa mora vedno kazati na tistega, ki je dejansko podpisal — zgodovina je obvezna, ne le trenutno stanje.

| Polje | Tip | Omejitve |
|---|---|---|
| `company` | FK → Company | |
| `ime_priimek` | CharField | required |
| `funkcija` | CharField | optional |
| `podpis_slika` | FileField | JPG/PNG, tip preverjen po vsebini datoteke, ne po končnici |
| `velja_od` | DateField | required |
| `velja_do` | DateField | `null=True` — `null` = trenutno veljaven |

Omejitev (validirana v `clean()`, ne DB constraint — SQLite ne podpira pogojnih unique): na `company` sme biti kvečjemu en zapis z `velja_do IS NULL` naenkrat.

**NumberSequence**

*Zakaj ločen model, ne polje na `Company`.* Dve neodvisni zaporedji (NP, ST) z različnim obnašanjem resetiranja (§2.4) potrebujeta lasten prostor za `zadnja_stevilka`, ne moreta biti dve polji na `Company`, ker bi to podvojilo logiko za vsak nov tip dokumenta.

| Polje | Tip | Omejitve |
|---|---|---|
| `company` | FK → Company | |
| `tip` | CharField, choices: `NP`/`ST` | |
| `leto` | IntegerField | `null=True` — obvezno pri `ST`, `null` pri `NP` |
| `zadnja_stevilka` | IntegerField | default `0` |

`unique_together = (company, tip, leto)`.

### 1.2 `catalogs`

**Zakaj šifranti, ne zakodiran nabor.** DDV režimi in storitve se s časom spreminjajo (nova stopnja, nova klavzula) — če bi bili v kodi, bi vsaka sprememba pomenila novo izdajo aplikacije. Način obračuna (spodaj) je izjema: ta ostaja v kodi namerno, ker vsak nov način pomeni novo testirano logiko izračuna, ne le nov podatek.

**VatRegime**

| Polje | Tip | Omejitve |
|---|---|---|
| `sifra` | CharField | unique |
| `naziv` | CharField | required |
| `nacin_obracuna` | CharField, choices: `OBDAVCENO`/`OPROSCENO`/`NI_PREDMET`/`OBRNJENA_DAVCNA` | zaprt nabor v kodi (Django `TextChoices`) |
| `stopnja` | DecimalField(5,2) | required |
| `klavzula` | TextField | required, kadar `nacin_obracuna` ∈ {`OPROSCENO`, `OBRNJENA_DAVCNA`} |
| `rekapitulacijska_skupina` | CharField | required |
| `zahteva_referenco` | BooleanField | default `False` |
| `velja_od` | DateField | required |
| `velja_do` | DateField | `null=True` |
| `eslog_koda` | CharField | optional v fazi 1, polni se v fazi 2 |
| `aktiven` | BooleanField | default `True` |

**Pravilo:** `clean()` prepreči `aktiven=False`, dokler obstaja aktivna `LeaseItem` s to referenco (dopolnilo 10). *Zakaj:* napako je ceneje preprečiti ob viru (en klik v adminu) kot loviti mesec dni kasneje kot izjemo v paketnem pregledu.

**ServiceCatalogItem**

| Polje | Tip | Omejitve |
|---|---|---|
| `naziv` | CharField | required |
| `privzeta_cena` | DecimalField(10,2) | `null=True` |
| `privzeti_ddv_rezim` | FK → VatRegime | `null=True` |
| `enota_mere` | CharField | prosto besedilo (kos/ura/m²/mesec…) — ne ločen šifrant |
| `aktivna` | BooleanField | default `True` |

### 1.3 `contracts`

**Zakaj `Lease` in ne `Tenant` nosi zgodovino trajanja, indeksacije in postavk.** Najemnik lahko ima več pogodb (različne enote, različni pogoji); pogodba je enota, ki se dejansko zaračunava, ne najemnik kot tak.

**Property** / **Unit**

| Model | Polje | Tip | Omejitve |
|---|---|---|---|
| Property | `company` | FK → Company | |
| | `naziv` | CharField | required |
| | `naslov` | CharField | required |
| | `aktivna` | BooleanField | default `True` |
| Unit | `property` | FK → Property | |
| | `oznaka` | CharField | required, npr. "Pisarna 1" |
| | `tip_enote` | CharField, choices: `PISARNA`/`POSLOVNI_PROSTOR`/`SKLADISCE`/`PARKIRNO_MESTO`/`GARAZA`/`OPREMA`/`DRUGO` | uporabljeno za privzeti DDV režim, PRD Dodatek B |
| | `povrsina_m2` | DecimalField(8,2) | `null=True` |
| | `aktivna` | BooleanField | default `True` |

**Tenant**

*Zakaj `email` na `Tenant`, ločeno od `RecipientContact` na pogodbi.* Odkrito med pisanjem §3 — validacija e-naslova (§6.9 PRD) se dogaja ob vnosu partnerja, preden sploh obstaja pogodba. `RecipientContact` (spodaj) je vezan na `Lease` in ureja dostavne kanale (TO/CC/BCC); `Tenant.email` je splošni kontaktni podatek, ki ga `RecipientContact` privzeto predlaga ob ustvarjanju nove pogodbe.

| Polje | Tip | Omejitve |
|---|---|---|
| `tip` | CharField, choices: `PRAVNA_OSEBA`/`FIZICNA_OSEBA` | required |
| `naziv` | CharField | required |
| `davcna_stevilka` | CharField | required če `PRAVNA_OSEBA`; modul 11 (§3) kadar izpolnjeno, ne glede na tip |
| `maticna_stevilka` | CharField | required če `PRAVNA_OSEBA`, natanko 10 mest |
| `naslov` | CharField | required |
| `ddv_zavezanec_s_polnim_odbitkom` | BooleanField | default `False` — vhod v validacijo §2.6 |
| `email` | EmailField | required (dopolnilo 11) |
| `aktiven` | BooleanField | default `True` |

**Lease**

*Zakaj polni nabor polj trajanja živi tukaj, čeprav se logika, ki se nanje odziva, izvede šele v poznejši fazi.* Polja so poceni, migracija na osrednji entiteti kasneje ni. Glej Prilogo B, dopolnilo 1.

| Polje | Tip | Omejitve |
|---|---|---|
| `unit` | FK → Unit | |
| `tenant` | FK → Tenant | |
| `oznaka_pogodbe` | CharField | required — dejanska št. iz podpisane pogodbe |
| `tip_trajanja` | CharField, choices: `DOLOCEN_CAS`/`NEDOLOCEN_CAS` | required |
| `datum_zacetka` | DateField | required |
| `datum_konca` | DateField | required če `DOLOCEN_CAS`, mora biti `null` če `NEDOLOCEN_CAS` |
| `samodejno_podaljsanje` | BooleanField | default `False` |
| `obdobje_podaljsanja_mesecev` | IntegerField | required če `samodejno_podaljsanje=True` |
| `odpovedni_rok_mesecev` | IntegerField | `null=True`, informativno |
| `datum_odpovedi` | DateField | `null=True` |
| `datum_prenehanja` | DateField | `null=True` — zadnji dan zaračunavanja |
| `status` | CharField, choices: `AKTIVNA`/`ODPOVEDANA`/`PRENEHALA` | default `AKTIVNA` |
| `rok_placila_dni` | IntegerField | default iz `Company.rok_placila_dni_privzeto` |
| `ima_klavzulo_indeksacije` | BooleanField | default `False` |
| `opis_pogoja_indeksacije` | TextField | dokumentarno besedilo za človeka, ni vhod v izračun |
| `tip_pogoja_indeksacije` | CharField, choices: `VEDNO`/`PRAG_SPREMEMBE`/`SAMO_NAVZGOR` | required če `ima_klavzulo_indeksacije=True` |
| `prag_spremembe_odstotek` | DecimalField(5,2) | required če `tip_pogoja_indeksacije='PRAG_SPREMEMBE'` |

**LeaseItem**

*Zakaj `aktivna` (boolean) ni v modelu.* Prvotno je bilo — odstranjeno, ker bi dva neodvisna vira resnice (boolean in datum) prej ali slej razpadla narazen. Stanje "ali se postavka zdaj zaračunava" je izpeljano iz datumov, ne shranjeno.

| Polje | Tip | Omejitve |
|---|---|---|
| `lease` | FK → Lease | |
| `naziv_postavke` | CharField | default "Najemnina" |
| `znesek_neto_mesecno` | DecimalField(10,2) | required, `MinValueValidator(0.01)` — striktno pozitivno, trajno pravilo (potrjeno, popusti ne obstajajo v praksi) |
| `vat_regime` | FK → VatRegime | required |
| `referenca_listine` | CharField | required če `vat_regime.zahteva_referenco` |
| `datum_reference` | DateField | required skupaj z zgornjim |
| `datum_zacetka_postavke` | DateField | `null=True` — `null` = enako kot `Lease.datum_zacetka`. Potrjeno kot realen scenarij (dopolnilo 5) |
| `datum_prenehanja_postavke` | DateField | `null=True` — zadnji dan zaračunavanja te postavke. Potrjeno kot realen scenarij (dopolnilo 4) |
| `indeks_ob_osnovi` | DecimalField(6,3) | required če `lease.ima_klavzulo_indeksacije=True` — glej §4 za natančen pomen (referenčna točka, ne nujno sklenitev) |
| `datum_osnove_indeksacije` | DateField | required skupaj z zgornjim |
| `osnovna_najemnina_ob_sklenitvi` | DecimalField(10,2) | informativno/revizijsko, ni vhod v izračun |

Pravilo (`clean()`): kadar `lease.ima_klavzulo_indeksacije=True`, `indeks_ob_osnovi` in `datum_osnove_indeksacije` ne smeta biti prazna — prepreči se ob shranjevanju (dopolnilo 14).

**IndexationEvent**

*Zakaj na `LeaseItem`, ne na `Lease`.* Pogodba ima lahko več postavk z različnimi DDV režimi (pisarna + parkirno mesto); indeksacija na nivoju cele pogodbe ne bi bila jasna, ko imata postavki različno osnovo.

| Polje | Tip | Omejitve |
|---|---|---|
| `lease_item` | FK → LeaseItem | |
| `datum_uskladitve` | DateField | required — privzeto **1.2.{leto}**, ne 1.1. (§4 — januarska faktura je vedno izdana pred objavo indeksa) |
| `indeks_vrednost` | DecimalField(6,3) | required |
| `stara_vrednost` | DecimalField(10,2) | required |
| `nova_vrednost` | DecimalField(10,2) | required |
| `izkljucen` | BooleanField | default `False` |
| `razlog_izkljucitve` | TextField | required če `izkljucen=True` |

**PoracunIndeksacije** *(dopolnilo 15 — glej §4.2)*

*Zakaj ta model obstaja.* Postopek indeksacije se po delovnem vzorcu izvede konec januarja, ko je indeks objavljen — februarska faktura ga že odraža. Če uporabnik indeks vnese kasneje (npr. marca), so bile vmesne fakture (februar) že izdane po stari vrednosti. Načelo posnetka prepoveduje spreminjanje že izdanih računov — poračun mora biti nova, ločena postavka na naslednjem računu, ne popravek starega.

| Polje | Tip | Omejitve |
|---|---|---|
| `indexation_event` | FK → IndexationEvent | |
| `original_invoice` | FK → Invoice | že izdana faktura po stari vrednosti |
| `prizadeto_obdobje_od` / `prizadeto_obdobje_do` | DateField ×2 | mesec, ki je premalo zaračunan |
| `znesek_poracuna` | DecimalField(10,2) | `nova_vrednost - stara_vrednost` |
| `status` | CharField, choices: `PREDLAGAN`/`POTRJEN`/`ZAVRNJEN` | default `PREDLAGAN` |
| `razlog_zavrnitve` | TextField | required če `status='ZAVRNJEN'` |
| `vkljucen_v_racun` | FK → Invoice | `null=True` — nastavi se, ko je dejansko dodan kot vrstica |

**RecipientContact**

| Polje | Tip | Omejitve |
|---|---|---|
| `lease` | FK → Lease | |
| `email` | EmailField | required |
| `vloga` | CharField, choices: `TO`/`CC`/`BCC` | required |
| `kanal` | CharField, choices: `PDF`/`ESLOG`/`OBOJE` | required; `ESLOG`/`OBOJE` prepovedan, če `lease.tenant.tip='FIZICNA_OSEBA'` (PRD P0-22) |
| `aktiven` | BooleanField | default `True` |

*Opomba o lastništvu modela:* SAD §4 (izvirna tabela entitet) `RecipientContact` uvršča pod app `delivery`. Ta specifikacija ga postavlja v `contracts`, ker je podatek, vnesen ob urejanju pogodbe, ne ob pošiljanju. Glej Prilogo C za popravek, potreben v SAD.

### 1.4 `invoicing` (jedro)

**Zakaj `MonthlyBatch` ni v tem dokumentu.** Δ-primerjava potrebuje samo pretekle `Invoice` zapise, ne orkestracijski model — `MonthlyBatch` sledi *kateremu koraku UI cikla* si trenutno na, kar je vprašanje za fazo 3, ne za domensko plast.

**Invoice**

*Zakaj toliko polj podvaja `Company`/`Tenant`/`Lease`.* Načelo posnetka (PRD §6.1): izdan račun je davčni dokument, ne sme se spremeniti, če se spremeni pogodba, šifrant ali podatki podjetja. Vsako polje spodaj, označeno "posnetek", je namerna denormalizacija.

| Polje | Tip | Omejitve |
|---|---|---|
| `company` | FK → Company | |
| `document_type` | CharField, choices: `NP`/`ST`/`STORNO`/`DOBROPIS` | required |
| `lease` | FK → Lease | `null=True` — `null` pri `ST` |
| `stevilka` | CharField | `null=True` do potrditve |
| `datum_izdaje` | DateField | required |
| `obdobje_od` / `obdobje_do` | DateField ×2 | required |
| `rok_placila` | DateField | izračunan ob potrditvi |
| `prejemnik_naziv` / `_naslov` / `_davcna_stevilka` / `_maticna_stevilka` | CharField ×4 | **posnetek** |
| `izdajatelj_naziv` / `_naslov` / `_davcna_stevilka` / `_maticna_stevilka` / `_trr` | CharField ×5 | **posnetek** |
| `izdajatelj_podpisnik_ime` / `_funkcija` / `_slika_pot` | CharField ×3 | **posnetek** veljavne verzije `Signatory` |
| `oznaka_pogodbe` | CharField | `null=True` pri `ST` |
| `objekt_naziv` | CharField | `null=True` |
| `status` | CharField, choices: `OSNUTEK`/`ZA_PONOVNI_PREGLED`/`IZBRAN_ZA_POSILJANJE`/`POSLAN`/`NAPAKA`/`ARHIVIRAN_NI_IZDAN`/`STORNIRAN`/`POPRAVLJEN` | default `OSNUTEK` — v fazi 1 relevantna dejansko samo `OSNUTEK` in `ARHIVIRAN_NI_IZDAN` (slednji za migracijske zapise, §7) |
| `povezan_racun` | FK → self | `null=True` — za STORNO/DOBROPIS/POPRAVLJEN |
| `razlog` | TextField | required kadar `document_type` ∈ {STORNO, DOBROPIS} |
| `razlog_arhiviranja` | TextField | `null=True`; standardna vrednost `"Migracijski zapis – uvoz stanja pred prehodom na aplikacijo"` za sintetične zapise (§7) |
| `osnova_skupaj` / `ddv_skupaj` / `skupaj_z_ddv` | DecimalField(10,2) ×3 | izračunano ob potrditvi |
| `rendered_pdf_path` / `rendered_eslog_path` / `checksum` | CharField ×3 | polni se v fazi 2 |

**InvoiceLine**

| Polje | Tip | Omejitve |
|---|---|---|
| `invoice` | FK → Invoice | |
| `lease` | FK → Lease | `null=True` — **samo sledljivost**, ni vir vrednosti |
| `opis` | TextField | required, sestavljen niz (PRD §6.11), shranjen dobesedno |
| `osnova` | DecimalField(10,2) | **posnetek** |
| `ddv_stopnja` | DecimalField(5,2) | **posnetek** |
| `ddv_znesek` | DecimalField(10,2) | |
| `skupaj` | DecimalField(10,2) | |
| `ddv_nacin_obracuna` | CharField | **posnetek** |
| `ddv_klavzula` | TextField | **posnetek** |
| `ddv_rekap_skupina` | CharField | **posnetek** |
| `razlog_rocnega_popravka` | TextField | `null=True`, required če je vrednost povožena |
| `izvirno_izracunana_vrednost` | DecimalField(10,2) | `null=True` |

### 1.5 `core`

**AuditLogEntry**

*Zakaj generično, ne poseben model za vsak primer.* Vstopna točka je bila ena: dispečer sprožilca "znesek se razlikuje" (§2.5) mora znati pojasniti ročni popravek zneska, ki se je zgodil mimo indeksacije ali sorazmernega obračuna. Isti mehanizem se je nato izkazal za dovolj splošnega za vse ročne popravke na `LeaseItem` (dopolnila 2, 4, 5) in za razveljavitev indeksacije (§4).

*Obseg v fazi 1 je namenoma ozek — pojasnilo obveznosti.* PRD P0-26 zahteva revizijsko sled "za vsako spremembo", kar je splošneje, kot ta model trenutno pokriva (samo `LeaseItem.znesek_neto_mesecno`, `datum_zacetka_postavke`, `datum_prenehanja_postavke`). To ni spregled — P0-26 je zahteva za **končni izdelek** (vseh pet faz), SAD §3 pa `audit` (generično, prek Django signalov, čez vse modele) eksplicitno umešča kot svoj app, ločen od faze 1. Ta model je isti model, ki ga bo `audit` app kasneje razširil na splošno rabo — faza 1 uporablja ozek podnabor, ki ga dejansko potrebuje.

| Polje | Tip | Omejitve |
|---|---|---|
| `content_type` | FK → ContentType | Django `contenttypes` |
| `object_id` | PositiveIntegerField | |
| `polje` | CharField | ime spremenjenega polja |
| `stara_vrednost` | CharField | stringificirano |
| `nova_vrednost` | CharField | stringificirano |
| `razlog` | TextField | required |
| `created_at` | DateTimeField | `auto_now_add=True` |

Index: `(content_type, object_id, polje, created_at)`.

**`core.pravila.zaokrozi()`**

*Zakaj ena funkcija, ne razpršeni klici `quantize()`.* Če se konvencija zaokroževanja izkaže za napačno, je popravek na enem mestu.

```python
from decimal import Decimal, ROUND_HALF_UP

def zaokrozi(vrednost: Decimal) -> Decimal:
    """Aritmetično zaokroževanje na cent. Samo za denarne vrednosti."""
    return vrednost.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
```

`ROUND_HALF_UP` je dokončna odločitev (potrjeno) — univerzalna slovenska/EU računovodska konvencija.

**`core.pravila.slugify_naziv()`**

*Zakaj ta funkcija rešuje PRD O2.* Ime datoteke (§9.2, §11.2 PRD) potrebuje `{podjetje}` kot niz brez šumnikov, pik in presledkov, ki je hkrati varen za datotečni sistem in prepoznaven človeku. Odločitev (potrjeno): poln naziv, ne kratica ali šifra — funkcija ga izpelje iz `Company.naziv` ob vsakem generiranju imena, ni shranjen kot ločeno polje.

```python
import re

_SUMNIKI = str.maketrans('čšžČŠŽ', 'cszCSZ')

def slugify_naziv(naziv: str) -> str:
    """Company.naziv -> del imena datoteke. Ohrani izvirno velikost črk,
    odstrani šumnike in pike, presledke zamenja z vezaji."""
    brez_sumnikov = naziv.translate(_SUMNIKI)
    brez_pik = brez_sumnikov.replace('.', '')
    return re.sub(r'\s+', '-', brez_pik.strip())
```

Preverjeno na PRD-jevem lastnem primeru podjetja "Alfa d.o.o." (§7.3 PRD): `slugify_naziv("Alfa d.o.o.")` → `"Alfa-doo"` — natanko ujemanje s primerom imena datoteke v §9.2 PRD (`2026-07-Alfa-doo.csv`). Za daljši naziv, npr. "Alfa nepremičnine d.o.o." (primer iz UIUX prototipov), bi rezultat bil `"Alfa-nepremicnine-doo"` — dejanska dolžina imena datoteke sledi dejanski dolžini `Company.naziv`, kar je posledica odločitve za poln naziv, ne omejitev funkcije.

**Izpuščeno iz faze 1** (obstaja v SAD §4, ne v tem dokumentu): `MonthlyBatch`, `DeliveryAttempt`, `AccountantReport`, `ArchivedFile`/`BackupSnapshot`, `LeaseDocument` (priloge — PRD eksplicitno P1/faza 3).

---

## 2. Pravila izračuna

### 2.1 DDV izračun, vključno mešani

**Zakaj mešana faktura ni poseben primer.** Vsaka `LeaseItem` se izračuna neodvisno; faktura je samo vsota svojih vrstic. En zapleten poseben primer bi bil vir napak — namesto tega ni posebnega primera.

```python
osnova = izracunaj_osnovo(znesek_neto_mesecno, obdobje, item)  # §2.2
ddv_znesek = zaokrozi(osnova * item.vat_regime.stopnja / 100)
skupaj = osnova + ddv_znesek
```

Po `nacin_obracuna`:

| Način | `ddv_znesek` | V rekapitulaciji |
|---|---|---|
| `OBDAVCENO` | zgornja formula | da, ločena vrstica po stopnji |
| `OPROSCENO` | `0.00` | da, s klavzulo |
| `NI_PREDMET` | `0.00` | ne — samo v `skupaj_z_ddv` |
| `OBRNJENA_DAVCNA` | `0.00` | da, s klavzulo o prenosu obveznosti |

```python
osnova_skupaj = sum(l.osnova for l in lines)
ddv_skupaj = sum(l.ddv_znesek for l in lines)
skupaj_z_ddv = osnova_skupaj + ddv_skupaj
```

**Robni primer, obvezen test:** `Lease` brez aktivnih postavk za dano obdobje ne vrne prazne fakture — domenska funkcija vrne eksplicitno "ni izdan" z razlogom (vhod za sprožilec §2.5, trigger #8 naslednji mesec).

### 2.2 Sorazmerni obračun

**Zakaj ena funkcija za pogodbo in za postavko.** Prekinitev cele pogodbe sredi meseca in prekinitev ene postavke (npr. opuščeno parkirno mesto) sta matematično isti problem — razlikujeta se samo argumenta.

```python
def dnevi_v_obdobju(obdobje_od, obdobje_do, entiteta_zacetek, entiteta_konec):
    ef_zacetek = max(obdobje_od, entiteta_zacetek) if entiteta_zacetek else obdobje_od
    ef_konec = min(obdobje_do, entiteta_konec) if entiteta_konec else obdobje_do
    if ef_zacetek > ef_konec:
        return 0
    return (ef_konec - ef_zacetek).days + 1

dnevi_v_mesecu = (obdobje_do - obdobje_od).days + 1
dnevi_najema = dnevi_v_obdobju(obdobje_od, obdobje_do, efektivni_zacetek, efektivni_konec)

if dnevi_najema == dnevi_v_mesecu:
    osnova = znesek_neto_mesecno
elif dnevi_najema == 0:
    osnova = None  # entiteta ne velja v tem obdobju
else:
    osnova = zaokrozi(znesek_neto_mesecno * Decimal(dnevi_najema) / Decimal(dnevi_v_mesecu))
```

Za pogodbo: `entiteta_zacetek/konec` = `datum_zacetka`/(`datum_prenehanja` če ni `AKTIVNA`, sicer `datum_konca`). Za postavko: `datum_zacetka_postavke`/`datum_prenehanja_postavke`, dodatno omejeno z veljavnostjo pogodbe. **Oba primera sta zdaj potrjena kot realna** (Priloga A), ne teoretična — testi zanju imajo enako prioriteto kot osnovni izračun.

**Ročni popravek:** `InvoiceLine.izvirno_izracunana_vrednost` shrani sistemsko vrednost pred povozom; `razlog_rocnega_popravka` obvezen. Vsaka vrstica z `dnevi_najema != dnevi_v_mesecu` dobi `je_sorazmerno=True` in **vedno** sproži izjemo — brez izjeme.

**Obvezni testni primeri:** vse dolžine meseca (28/29/30/31), začetek/konec na robu meseca, kratkotrajna pogodba znotraj enega meseca, `NEDOLOCEN_CAS` brez konca, postavka dodana ali prekinjena sredi pogodbe.

### 2.3 Izračun nazaj iz bruto zneska

**Zakaj DDV ni izračunan neodvisno.** Neodvisno zaokroževanje osnove in DDV lahko odstopa za cent od vnesenega bruto zneska — nesprejemljivo na davčnem dokumentu. DDV je definiran kot preostanek, ne kot ločen izračun, s čimer je vsota zajamčeno natančna.

```python
def izracunaj_iz_bruto(bruto, stopnja):
    osnova = zaokrozi(bruto / (1 + stopnja / Decimal(100)))
    ddv = bruto - osnova
    return osnova, ddv
```

Uporabljeno v ročnih storitvenih fakturah (faza 5 kot celoten tok; funkcija je obvezen test faze 1).

### 2.4 Številčenje in atomarnost

**Zakaj cela dodelitev in status v eni transakciji.** Če transakcija propade, se števec povrne — nobena številka ni "zažgana". Če uspe, je številka trajno porabljena, tudi če poznejši koraki (PDF) spodletijo — ponovni poskus uporabi isto številko, ne kliče funkcije znova.

```python
with transaction.atomic():
    leto = datum_izdaje.year if tip == 'ST' else None
    seq, _ = NumberSequence.objects.select_for_update().get_or_create(
        company=company, tip=tip, leto=leto, defaults={'zadnja_stevilka': 0}
    )
    seq.zadnja_stevilka += 1
    seq.save()
    invoice.stevilka = format_stevilka(tip, seq.zadnja_stevilka, leto)
    invoice.status = 'IZBRAN_ZA_POSILJANJE'
    invoice.save()
```

`select_for_update()` je dovolj za SQLite pri enem pisalnem procesu (SAD §5, P0-14). `STORNO`/`DOBROPIS` gresta skozi isto funkcijo, isti `tip` kot izvirnik — ni ločenega števca.

### 2.5 Δ zaznava — dispečer in predpogoji podjetja

**Zakaj sta dva ločena mehanizma, ne eden.** Manjkajoč podpisnik in nepotrjena razporeditev DDV režimov nista lastnost ene pogodbe — sta lastnost podjetja. Preverjati ju za vsako od 15 pogodb posebej bi bilo počasno in zavajajoče (15× ista napaka). Zato: en predpogojni pregled na podjetje (korak 2, PRD §7.4), en dispečer na pogodbo (korak 3).

**Predpogoji podjetja** (korak 2 — "predhodna kontrola"):

```python
def preveri_predpogoje_podjetja(company, datum_izdaje) -> list[Blokada]:
    blokade = []
    if not company.ddv_razporeditev_potrjena:
        blokade.append(Blokada("DDV razporeditev enot še ni potrjena pri računovodji — Nastavitve"))
    if not Signatory.objects.filter(company=company, velja_od__lte=datum_izdaje).filter(
        Q(velja_do__isnull=True) | Q(velja_do__gte=datum_izdaje)
    ).exists():
        blokade.append(Blokada(f"Ni veljavnega podpisnika za {company.naziv} na {datum_izdaje}"))
    return blokade
```

Če seznam ni prazen, korak 2 blokira nadaljevanje **za celo podjetje** — uporabnik se preusmeri v Nastavitve, ne v paketni pregled z 15 ponovljenimi opozorili.

**Δ dispečer** (korak 3, na nivoju pogodbe — predpostavlja, da je korak 2 uspel):

```python
def izracunaj_kandidata(lease, obdobje_od, obdobje_do, datum_izdaje) -> RacunKandidat:
    # vrne: vrstice, skupaj_z_ddv, se_izda, izjeme: list[Izjema]
```

Za sprožilec "znesek se razlikuje", dispečer preveri po vrsti: `IndexationEvent` ta mesec? → prikaži. Sorazmerni obračun (`je_sorazmerno`)? → že pokrito ločeno, ne podvoji. Nova postavka (`datum_zacetka_postavke` pade v to obdobje)? → "Nova postavka na pogodbi (dodana {datum})". Sprememba `vat_regime`? → staro/novo. Sicer poizvedba na `AuditLogEntry` za `znesek_neto_mesecno` od zadnjega izdanega računa → prazen rezultat → "vzrok neznan – preveri ročno" (pošten signal, ne prikrita gotovost).

"Zadnji izdan račun" za primerjavo obstaja od prvega meseca v produkciji dalje — glej §7 za mehanizem, ki to zagotovi tudi za 48 pogodb, ki v aplikaciji še nimajo zgodovine.

### 2.6 Meje veljavnosti pogodbe

**Zakaj pogodba, ki ne velja, izgine iz seznama, ne ostane vidna kot izjema vsak mesec.** Enkrat ugotovljeno "`dnevi_najema == 0`" velja za vsa prihodnja obdobja — prikazovanje v nedogled bi bilo šum, ne signal.

Uporablja `dnevi_v_obdobju()` iz §2.2 z argumenti pogodbe. `dnevi_najema == 0` → `se_izda=False`, `razlog_neizdaje="Pogodba ne velja v tem obdobju"`, izpade iz kandidatnega seznama za vsa naslednja obdobja.

### 2.7 Zgodovinskost šifranta

**Zakaj `InvoiceLine` ne referencira `VatRegime` za vrednosti.** To je edino mesto v modelu, kjer je normalizacija namerno opuščena — davčni dokument mora ostati nespremenjen, tudi če se šifrant kasneje popravi.

Obvezen test: ustvari fakturo, spremeni `VatRegime.stopnja`/`klavzula` na isti šifrantski vrstici, ponovno prikliči `InvoiceLine`, preveri nespremenjenost.

### 2.8 Storno / dobropis

**Zakaj je to edino mesto v pravilih izračuna, ki je predpostavka, ne izpeljava iz PRD.** PRD pove le, da si stornirani dokument in dobropis delita zaporedje z izvirnikom (§6.5) in da se izvirnik ne popravlja (P0-27) — ne pove, kako se izračuna znesek dobropisa. Predlagano pravilo je potrjeno (Priloga A).

- `STORNO`: nova `Invoice` z `document_type='STORNO'`, `povezan_racun=izvirnik`, vrstice zrcalijo izvirnik (isti opis, `osnova`/`ddv_znesek`/`skupaj` z negativnim predznakom), `razlog` obvezen.
- `DOBROPIS`: ista mehanika, a zneski se **ročno vnesejo** — edini dokument v modelu, kjer se osnova ne izračuna iz pogodbe. Vedno obvezen posamični pregled.

---

## 3. Validacija partnerjev

**Zakaj sploh implementirati kontrolno števko, ne le dolžino.** Napačno vtipkana davčna številka na izdanem računu je davčna napaka, ne kozmetična — modul 11 jo ujame ob vnosu, preden postane problem na dokumentu.

**Davčna številka.** Osemmestna, kontrolirana po modulu 11: prvih sedem mest je osnova, osmo je kontrolna števka.

```python
def preveri_davcno_stevilko(stevilka: str) -> bool:
    if not (len(stevilka) == 8 and stevilka.isdigit()):
        return False
    cifre = [int(c) for c in stevilka]
    ponderji = [8, 7, 6, 5, 4, 3, 2]
    vsota = sum(c * p for c, p in zip(cifre[:7], ponderji))
    ostanek = vsota % 11
    if ostanek == 0:
        return False  # osnovna številka uradno izključena iz nabora veljavnih
    kontrolna = 11 - ostanek
    if kontrolna == 10:
        kontrolna = 0
    return kontrolna == cifre[7]
```

Preverjeno na znanem primeru: osnova `1501255` → kontrolna `7` → `15012557`. Velja enako za fizične in pravne osebe (isti algoritem).

**Matična številka.** Potrjeno (Priloga A): vedno 10-mestna oblika (7-mestna osnova + 3-mestna pripona poslovne enote).

```python
def preveri_maticno_stevilko(stevilka: str) -> bool:
    return stevilka.isdigit() and len(stevilka) == 10
```

**Obveznost polj — asimetrija.** PRD zahteva davčno in matično samo za pravne osebe; o fizičnih molči, kar implicira, da so lahko brez njiju.

```python
def clean(self):
    if self.tip == 'PRAVNA_OSEBA':
        if not self.davcna_stevilka or not self.maticna_stevilka:
            raise ValidationError("Pravna oseba mora imeti davčno in matično številko.")
    if self.davcna_stevilka and not preveri_davcno_stevilko(self.davcna_stevilka):
        raise ValidationError("Neveljavna davčna številka (napačna kontrolna števka).")
```

**E-naslov — opozorilo, ne blokada.** PRD eksplicitno zahteva "opozorilo", ne "prepoved" pri podvojenih naslovih:

```python
def preveri_podvojen_email(email, izkljuci_tenant_id=None):
    obstaja = Tenant.objects.filter(email=email, aktiven=True).exclude(pk=izkljuci_tenant_id).exists()
    if obstaja:
        messages.warning(request, f"E-naslov {email} je že v uporabi pri drugem aktivnem najemniku.")
    # shranjevanje se NE prekine
```

**Dostavni kanal po tipu osebe (PRD P0-22).** `RecipientContact.clean()`:

```python
def clean(self):
    if self.lease.tenant.tip == 'FIZICNA_OSEBA' and self.kanal in ('ESLOG', 'OBOJE'):
        raise ValidationError("eSLOG dostava ni na voljo za fizične osebe.")
```

**AJPES/VIES.** Brez spremembe modela — `davcna_stevilka`/`maticna_stevilka` kot navadna `CharField` so že vmesnik za prihodnjo integracijo (asinhrona poizvedba, ki polja predizpolni).

---

## 4. Januarska indeksacija

**Zakaj je "pogoj izpolnjen" strukturiran, ne prosto besedilo.** PRD (§6.8) zahteva, da aplikacija *izračuna*, ali je pogoj izpolnjen — kar prosto besedilo po definiciji ne omogoča. Realne slovenske klavzule o indeksaciji poslovnih najemnin padejo v enega od treh vzorcev (vedno, prag spremembe, samo navzgor); `opis_pogoja_indeksacije` ostane dokumentarno besedilo za človeka.

**Zakaj verižna primerjava, ne fiksna baza.** PRD imenoma navaja "zadnja uporabljena vrednost indeksa" kot shranjen podatek — če bi bila baza vedno fiksna na sklenitev, to polje ne bi imelo funkcije. Verižni pristop ima dodatno prednost: če je bilo eno leto izključeno iz uskladitve, se naslednje leto primerjava samodejno "dohiti" brez posebnega popravka.

**Zakaj `datum_uskladitve` privzeto 1.2., ne 1.1.** Delovni vzorec: januarska faktura je izdana 1. januarja, po pravilu "obračun vnaprej" (§6.7 PRD) — indeks za preteklo leto pa je takrat še neobjavljen. Januar je torej **vedno** obračunan po stari vrednosti, ne glede na to, kdaj uporabnik vnese indeks; indeksacija dejansko prime šele s februarsko fakturo.

```python
def izracunaj_indeksacijo(lease_item, nov_indeks: Decimal, danes: date) -> IndeksacijaPredlog:
    lease = lease_item.lease
    if not lease.ima_klavzulo_indeksacije:
        return IndeksacijaPredlog(pogoj_izpolnjen=False, razlog="Pogodba nima klavzule o indeksaciji")

    zadnji = (IndexationEvent.objects
        .filter(lease_item=lease_item, izkljucen=False)
        .order_by('-datum_uskladitve').first())
    zadnji_indeks = zadnji.indeks_vrednost if zadnji else lease_item.indeks_ob_osnovi

    razmerje = nov_indeks / zadnji_indeks
    odstotek = (razmerje - 1) * 100

    if lease.tip_pogoja_indeksacije == 'PRAG_SPREMEMBE' and abs(odstotek) < lease.prag_spremembe_odstotek:
        return IndeksacijaPredlog(pogoj_izpolnjen=False, razlog=f"Sprememba {odstotek:.2f}% pod pragom {lease.prag_spremembe_odstotek}%")

    nova_vrednost = zaokrozi(lease_item.znesek_neto_mesecno * razmerje)

    if lease.tip_pogoja_indeksacije == 'SAMO_NAVZGOR' and nova_vrednost < lease_item.znesek_neto_mesecno:
        return IndeksacijaPredlog(pogoj_izpolnjen=False, razlog="Indeks padel — klavzula dovoljuje samo zvišanje")

    return IndeksacijaPredlog(
        pogoj_izpolnjen=True,
        stara_vrednost=lease_item.znesek_neto_mesecno,
        nova_vrednost=nova_vrednost,
        odstotek_spremembe=odstotek,
    )
```

**Razveljavitev** (PRD: "mogoča le, dokler po njej ni bil izdan noben račun") je guard v domenski funkciji, ne novo polje: preveri obstoj `InvoiceLine` s to `lease_item` po `datum_uskladitve`; če obstaja, zavrne. Sama razveljavitev piše `AuditLogEntry` z razlogom "Razveljavljena indeksacija #{id}" in povrne `znesek_neto_mesecno`.

### 4.1 Konkreten primer — 10 let, 2016–2025

**Zakaj ta primer uporablja resnične podatke.** Ponazoritev na izmišljenih odstotkih bi prikrila prav tisto, kar je pri verižnem pristopu vredno videti: leta z majhno spremembo (2016, 2018, 2024) se preskočijo, leto s padcem (2020) se preskoči brez škode, leto z izjemno inflacijo (2022) pa se v celoti odrazi. Uporabljene so dejanske letne stopnje inflacije za Slovenijo (vir: OECD/SURS, prek inflationtool.com), obdobje 2016–2025.

**Predpostavke primera:** `Lease` sklenjena 1. 1. 2016, `znesek_neto_mesecno = 1.000,00 €`, `indeks_ob_osnovi = 100,000`, `tip_pogoja_indeksacije = 'PRAG_SPREMEMBE'`, `prag_spremembe_odstotek = 2,00 %`.

**Indeksna veriga** (letna stopnja inflacije → kumulativni indeks, baza 100,000 na 1. 1. 2016):

| Leto | Letna stopnja | Indeks (31. 12.) |
|---|---|---|
| 2015 (baza) | — | 100,000 |
| 2016 | +0,51 % | 100,510 |
| 2017 | +1,74 % | 102,259 |
| 2018 | +1,42 % | 103,711 |
| 2019 | +1,84 % | 105,619 |
| 2020 | −1,09 % | 104,468 |
| 2021 | +4,89 % | 109,576 |
| 2022 | +10,25 % | 120,808 |
| 2023 | +4,16 % | 125,834 |
| 2024 | +1,86 % | 128,175 |
| 2025 | +2,71 % | 131,649 |

**Postopek, izračunan z `izracunaj_indeksacijo()` (verificirano, ne ročno ocenjeno). Vsak velja od 1. 2. navedenega leta:**

| Postopek | Primerjava | Sprememba | Odločitev | Najemnina od tega meseca dalje |
|---|---|---|---|---|
| od 1.2.2017 (za 2016) | 100,510 proti 100,000 | +0,51 % | pod pragom — ni izvedeno | 1.000,00 € |
| od 1.2.2018 (za 2017) | 102,259 proti 100,000 | +2,26 % | **izvedeno** | 1.022,59 € |
| od 1.2.2019 (za 2018) | 103,711 proti 102,259 | +1,42 % | pod pragom — ni izvedeno | 1.022,59 € |
| od 1.2.2020 (za 2019) | 105,619 proti 102,259 | +3,29 % | **izvedeno** | 1.056,19 € |
| od 1.2.2021 (za 2020) | 104,468 proti 105,619 | −1,09 % | pod pragom (padec) — ni izvedeno | 1.056,19 € |
| od 1.2.2022 (za 2021) | 109,576 proti 105,619 | +3,75 % | **izvedeno** | 1.095,76 € |
| od 1.2.2023 (za 2022) | 120,808 proti 109,576 | +10,25 % | **izvedeno** | 1.208,08 € |
| od 1.2.2024 (za 2023) | 125,834 proti 120,808 | +4,16 % | **izvedeno** | 1.258,34 € |
| od 1.2.2025 (za 2024) | 128,175 proti 125,834 | +1,86 % | pod pragom — ni izvedeno | 1.258,34 € |
| od 1.2.2026 (za 2025) | 131,649 proti 125,834 | +4,62 % | **izvedeno** | **1.316,49 €** |

**Kar ta primer dejansko pokaže:** od desetih priložnosti se je indeksacija izvedla šestkrat. Kljub štirim preskočenim letom je končna najemnina po desetih letih 1.316,49 € — natančno **31,65 %** rast, kar ustreza dejanski kumulativni inflaciji v tem obdobju. Nobena inflacija ni bila trajno izgubljena zaradi praga — vsako preskočeno leto se je "prenesla naprej" v naslednjo primerjavo. To je neposredna posledica verižnega dizajna, ne naključje.

Opozorilo glede natančnosti primera: indeksna veriga je rekonstruirana iz javno objavljenih letnih odstotkov, ne iz dejanskih točkovnih vrednosti indeksa. Za produkcijsko rabo se `nov_indeks` vedno vnese kot dejansko objavljena vrednost.

### 4.2 Poračun ob zamujenem vnosu (dopolnilo 15)

**Zaznava** — kliče se ob potrditvi vsake `IndexationEvent`, samodejno, ne na zahtevo:

```python
def zaznaj_zamujen_poracun(indexation_event) -> list[PoracunIndeksacije]:
    lease_item = indexation_event.lease_item
    ze_pokrita = PoracunIndeksacije.objects.filter(
        indexation_event=indexation_event
    ).values_list('prizadeto_obdobje_od', flat=True)

    prizadete = InvoiceLine.objects.filter(
        lease=lease_item.lease,
        invoice__obdobje_od__gte=indexation_event.datum_uskladitve,
        invoice__obdobje_od__lt=date.today().replace(day=1),
        invoice__status__in=['IZBRAN_ZA_POSILJANJE', 'POSLAN'],
    ).exclude(invoice__obdobje_od__in=ze_pokrita)

    razlika = indexation_event.nova_vrednost - indexation_event.stara_vrednost
    return [
        PoracunIndeksacije(
            indexation_event=indexation_event,
            original_invoice=line.invoice,
            prizadeto_obdobje_od=line.invoice.obdobje_od,
            prizadeto_obdobje_do=line.invoice.obdobje_do,
            znesek_poracuna=razlika,
            status='PREDLAGAN',
        )
        for line in prizadete
    ]
```

Potrditev (uporabniško da/ne, faza 3) vsak `PoracunIndeksacije` s `status='POTRJEN'` pretvori v **ločeno vrstico** na naslednji fakturi za to pogodbo — ne nov dokument. Opis je sestavljen po isti konvenciji kot §6.11 PRD:

```
Poračun indeksacije za obdobje 01.02.2026 – 28.02.2026
```

Vrstica podeduje `vat_regime` od izvirne `LeaseItem` in gre skozi isti DDV izračun kot vsaka druga vrstica (§2.1). Več prizadetih mesecev naenkrat (zamuda več kot en mesec) da več ločenih vrstic, vsako s svojim obdobjem.

---

## 5. Robni primeri

**Zakaj so nekateri od teh v ločenem seznamu, ne v pravilih izračuna.** To niso formule, so odločitve o tem, kaj se zgodi na robu — kjer PRD molči ali kjer je bila potrebna presoja, ne le prevod besedila v kodo.

**Šifrant med aktivno uporabo.** Glej §1.2, dopolnilo 10 — blokada deaktivacije, ne runtime odkritje.

**Manjkajoč podpisnik in nepotrjena razporeditev DDV.** Glej §2.5 — predpogoj podjetja, ne izjema na nivoju pogodbe.

**Preskočen mesec.** Δ primerjava referencira *zadnji dejansko izdan* račun, ne strogo prejšnji koledarski mesec. Če je zadnji izdan bil julij, se september primerja z julijem; avgust se pokaže prek istega sprožilca kot "prejšnji mesec arhiviran kot neizdan" (§2.5), čeprav sploh ni bil zagnan — enak signal, ista obravnava.

**Delno potrjen paket, prekinjen sredi seje.** Ni napaka — je nameravana posledica tega, da vsaka faktura nosi svojo atomarno transakcijo (§2.4), ne obstaja transakcija na nivoju celega paketa. En del ostane potrjen in nespremenljiv, drugi čaka. Transakcija na nivoju paketa bi to varnost odvzela, ne dodala.

**Migracija — začetne vrednosti zaporedij.** Najbolj tvegan enkraten robni primer v celotnem sistemu, a operativen, ne tehničen: če `NumberSequence.zadnja_stevilka` ob prvem zagonu ne ustreza dejanski zadnji izdani številki iz Excela, prva prava izdaja podvoji obstoječo številko na davčnem dokumentu. Test tega ne ujame, ker aplikacija nima podatka o pretekli zgodovini. Edina obramba: ročna nastavitev pred prvo produkcijsko izdajo za vsako od petih podjetij, zapisana v operativni priročnik (glej tudi §7).

**Ničelni sorazmerni znesek.** Kratkotrajna postavka se lahko po zaokrožitvi izračuna na `0,00` — realen, ne napačen rezultat. Izda se, `je_sorazmerno=True` jo že avtomatsko pošlje v pregled.

---

## 6. Struktura testov

**Zakaj so testi organizirani po Django app, ne po PRD poglavju.** Vsak test mora živeti ob kodi, ki jo preverja (`pytest` konvencija) — sledljivost do PRD je v opisu testa, ne v strukturi map.

**`core/tests/test_pravila.py`**
| Test | Preveri |
|---|---|
| `test_zaokrozi_pol_navzgor` | `0.005→0.01`, `0.015→0.02`, `0.025→0.03` |
| `test_slugify_naziv_sumniki_in_pike` | `"Alfa d.o.o."` → `"Alfa-doo"` (ujemanje s PRD §9.2 primerom) |

**`companies/tests/test_stevilcenje.py`**
| Test | Preveri |
|---|---|
| `test_atomarno_dodeljevanje_socasno` | Sočasen klic ne proizvede podvojene številke |
| `test_np_ne_resetira_cez_leto` | `NP` se nadaljuje čez 31.12./1.1. |
| `test_st_resetira_po_letu_izdaje` | `ST` se resetira glede na `datum_izdaje.year` |
| `test_stevilka_dodeljena_sele_ob_potrditvi` | Osnutek nima `stevilka` |
| `test_neuspesna_transakcija_ne_zazge_stevilke` | Propad transakcije povrne `zadnja_stevilka` |
| `test_storno_uporabi_isto_zaporedje` | `STORNO` dobi naslednjo številko istega niza |

**`companies/tests/test_predpogoji.py`**
| Test | Preveri |
|---|---|
| `test_nepotrjena_ddv_razporeditev_blokira` | `preveri_predpogoje_podjetja()` — dopolnilo 19 |
| `test_manjkajoc_podpisnik_blokira_celo_podjetje` | Blokada, ne 15 ponovljenih izjem |
| `test_veljaven_podpisnik_in_potrjena_razporeditev_prehod` | Prazen seznam blokad |

**`catalogs/tests/test_validacija_partnerjev.py`**
| Test | Preveri |
|---|---|
| `test_davcna_stevilka_veljavna_kontrolna` | Znan pravilen primer (`15012557`) |
| `test_davcna_stevilka_napacna_kontrolna` | Napačna zadnja cifra → `False` |
| `test_davcna_stevilka_ostanek_nic_neveljavna` | `vsota % 11 == 0` → `False` |
| `test_maticna_stevilka_10_mest` | Točno 10 mest, zavrne ostale dolžine |
| `test_pravna_oseba_brez_davcne_zavrnjena` | `clean()` prepreči shranjevanje |
| `test_fizicna_oseba_brez_davcne_dovoljena` | Namerna asimetrija |
| `test_podvojen_email_opozori_ne_blokira` | `messages.warning()`, shranjevanje uspe |
| `test_eslog_prepovedan_za_fizicno_osebo` | P0-22 |
| `test_vatregime_deaktivacija_blokirana_ce_v_uporabi` | §1.2 |

**`contracts/tests/test_veljavnost.py`**
| Test | Preveri |
|---|---|
| `test_zacetek_na_prvi_dan_poln_mesec` | `dnevi_najema == dnevi_v_mesecu` |
| `test_zacetek_na_zadnji_dan_en_dan` | `dnevi_najema == 1` |
| `test_konec_na_zadnji_dan_poln_mesec` | Simetrično zgornjemu |
| `test_kratkotrajna_pogodba_znotraj_meseca` | Začetek in konec v istem mesecu |
| `test_nedolocen_cas_brez_konca` | `datum_konca=None` → razteg do `obdobje_do` |
| `test_pogodba_zunaj_obdobja_ni_kandidat` | `dnevi_najema == 0` → izpade iz seznama |
| `test_postavka_dodana_sredi_pogodbe` | `datum_zacetka_postavke` — potrjen realen primer |
| `test_postavka_prekinjena_sredi_pogodbe` | `datum_prenehanja_postavke` — potrjen realen primer |

**`contracts/tests/test_sorazmerni_obracun.py`**
| Test | Preveri |
|---|---|
| `test_vse_dolzine_meseca` | Parametrizirano 28/29/30/31 |
| `test_simetricnost_zacetek_konec` | Enak dan-delež → enak znesek |
| `test_rocni_popravek_zahteva_razlog` | Obvezna polja ob povozu |
| `test_sorazmerno_vedno_sprozi_izjemo` | Ne glede na spremembo zneska |

**`invoicing/tests/test_ddv.py`**
| Test | Preveri |
|---|---|
| `test_obdavceno_izracun` | Osnova × stopnja = DDV |
| `test_oprosceno_nic_ddv_s_klavzulo` | `ddv_znesek=0`, klavzula neprazna |
| `test_ni_predmet_izven_rekapitulacije` | Izključeno iz rekapitulacije, v `skupaj_z_ddv` |
| `test_mesana_faktura_dve_stopnji` | Dve postavki, dve stopnji, ista faktura |
| `test_izracun_iz_bruto_natancnost` | `osnova + ddv == bruto`, vključno robne primere |
| `test_manjkajoca_referenca_blokira` | Izjema, ne tiha izdaja |

**`invoicing/tests/test_delta.py`**
| Test | Preveri |
|---|---|
| `test_prva_izdaja_izjema` | |
| `test_znesek_pojasnjen_z_indeksacijo` | Dispečer najde `IndexationEvent` |
| `test_znesek_pojasnjen_z_novo_postavko` | Dispečer prepozna `datum_zacetka_postavke` v obdobju |
| `test_znesek_pojasnjen_z_rocnim_popravkom` | Dispečer najde `AuditLogEntry` |
| `test_znesek_brez_pojasnila` | Brez razlage → "vzrok neznan – preveri ročno" |
| `test_preskocen_mesec_primerja_z_zadnjim_izdanim` | §5 |

**`invoicing/tests/test_zgodovinskost.py`**
| Test | Preveri |
|---|---|
| `test_sprememba_vatregime_ne_vpliva_na_izdan_racun` | |
| `test_sprememba_podpisnika_ne_vpliva_na_izdan_racun` | |

**`invoicing/tests/test_storno.py`**
| Test | Preveri |
|---|---|
| `test_storno_zrcali_vrstice` | Negativne vrednosti, `razlog` obvezen |
| `test_izvirnik_se_ne_spremeni` | |

**`contracts/tests/test_indeksacija.py`**
| Test | Preveri |
|---|---|
| `test_verizna_primerjava_ne_fiksna_baza` | Druga indeksacija proti lanski, ne izvirni |
| `test_izpuscen_letnik_samodejno_dohiti` | Glej §4.1 primer |
| `test_prag_spremembe_pod_pragom_ni_izpolnjen` | |
| `test_samo_navzgor_zavrne_padec` | |
| `test_manjkajoc_izhodiscni_indeks_zavrnjen_ob_shranjevanju` | `LeaseItem.clean()` |
| `test_razveljavitev_blokirana_po_izdanem_racunu` | |
| `test_razveljavitev_povrne_prejsnjo_vrednost` | |
| `test_datum_uskladitve_privzeto_prvi_februar` | §4 |

**`contracts/tests/test_poracun_indeksacije.py`**
| Test | Preveri |
|---|---|
| `test_zamuja_en_mesec_ena_vrstica_poracuna` | §4.2 |
| `test_zamuja_vec_mesecev_vec_locenih_vrstic` | Vsaka s svojim obdobjem |
| `test_ni_zamude_ni_poracuna` | Pravočasen vnos → prazen seznam |
| `test_idempotentnost_ponovnega_zaznavanja` | Že pokrita obdobja se ne podvojijo |

**`contracts/tests/test_migracija.py`**
| Test | Preveri |
|---|---|
| `test_sinteticen_racun_omogoci_pravilno_delta_primerjavo` | §7 — prvi pravi mesec ne sproži lažne "prva izdaja" |
| `test_sinteticen_racun_nima_stevilke` | `stevilka IS NULL`, `status='ARHIVIRAN_NI_IZDAN'` |
| `test_migracija_neodvisna_od_leta_pricetka` | Isti mehanizem deluje za poljuben mesec prehoda |

---

## 7. Prehod obstoječih pogodb na aplikacijo (migracija)

**Zakaj je ta razdelek v FSD, ne le v operativnem priročniku.** Vsi trije mehanizmi spodaj imajo posledice na domensko logiko (indeksacijska veriga, Δ dispečer) in morajo biti testirani kot del faze 1 — priročnik (PRD §16.3) opisuje *postopek*, ta razdelek definira *mehanizem*, na katerem postopek temelji.

### 7.1 En referenčni datum za dva mehanizma

Indeksacijska veriga (§4, `indeks_ob_osnovi`/`datum_osnove_indeksacije`) in Δ dispečer (§2.5, "zadnji izdan račun") oba potrebujeta "zadnje znano stanje pred prehodom". To ni naključje — to je **isti trenutek**, uporabljen dvakrat:

> **Referenčna točka migracije = zadnji mesec, dejansko zaračunan v Excelu, neposredno pred prvim mesecem v aplikaciji.**

Pravilo je univerzalno glede na leto začetka: ne "vedno december", ampak "kar koli je bilo zadnje pred preklopom" — če se preklop zgodi avgusta, je referenčna točka julij, ne lanski december.

`LeaseItem.indeks_ob_osnovi`/`datum_osnove_indeksacije` s tem dobita širši pomen kot "vrednost ob sklenitvi pogodbe" — za migrirano pogodbo pomenita "zadnja objavljena vrednost indeksa na referenčni datum prehoda" (dopolnilo 16). Za novo pogodbo, sklenjeno po prehodu, ostaneta dobesedno "ob sklenitvi".

### 7.2 Sintetični zapis za Δ dispečer (dopolnilo 17)

Brez tega bi vsaka od 48 pogodb ob prvem mesecu v aplikaciji sprožila sprožilec "prva izdaja" (§2.5) — čeprav gre za normalno nadaljevanje, ne novo pogodbo. Rešitev ni poseben primer v dispečerju, je en podatkovni zapis na pogodbo, ustvarjen enkrat, ob migraciji:

```python
def uvozi_migracijsko_stanje(lease, zadnje_zaracunano_obdobje_od, zadnje_zaracunano_obdobje_do):
    invoice = Invoice.objects.create(
        company=lease.unit.property.company,
        document_type='NP',
        lease=lease,
        status='ARHIVIRAN_NI_IZDAN',
        razlog_arhiviranja="Migracijski zapis – uvoz stanja pred prehodom na aplikacijo",
        datum_izdaje=zadnje_zaracunano_obdobje_od,
        obdobje_od=zadnje_zaracunano_obdobje_od,
        obdobje_do=zadnje_zaracunano_obdobje_do,
    )
    for item in lease.leaseitem_set.filter(datum_prenehanja_postavke__isnull=True):
        InvoiceLine.objects.create(
            invoice=invoice, lease=lease,
            osnova=item.znesek_neto_mesecno,
            # ddv_* izračunano iz item.vat_regime po §2.1
        )
    return invoice
```

Dispečer se s tem sploh ne spremeni — najde "prejšnji račun" in primerja normalno.

**Praktična poenostavitev:** ker sintetični zapis potrebuje iste zneske, ki jih tako ali tako vnašaš na `LeaseItem` (§7.1), se lahko izpelje samodejno iz že vnesenih podatkov — en ročni vnos (trenutna najemnina + izhodiščni indeks) služi obema mehanizmoma.

### 7.3 Kar ostaja operativno, ne v modelu

- **`NumberSequence.zadnja_stevilka`** — ročna nastavitev pred prvo izdajo, na podjetje (§5, robni primer).
- **`Signatory`** — vsaj en aktiven zapis na podjetje pred prvo izdajo (§2.5, predpogoj).
- **`Company.ddv_razporeditev_potrjena`** — ročna potrditev po pregledu z računovodjo (§1.1, PRD §16 postavka 5).
- **`VatRegime` šifrant** — privzeta vsebina že v PRD Dodatku A, standardna namestitev.

Vsi štirje spadajo v operativni priročnik (PRD §16.3) kot preverljiv seznam pred prvo produkcijsko izdajo — mehanizmi zanje so zdaj definirani v tem dokumentu, izvedba je enkraten ročni korak.

---

## Priloga A — Odprta vprašanja

**Vseh sedem vprašanj, odprtih skozi razvoj tega dokumenta, je zdaj zaprtih:** matična številka (10-mestna oblika), popusti (ne obstajajo, trajno pravilo), delna prekinitev/dodajanje postavke (potrjen realen scenarij), storno/dobropis pravilo (potrjeno), zaokroževanje (potrjen `ROUND_HALF_UP`), in nazadnje PRD-jevo lastno odprto vprašanje O2 (zapis naziva podjetja v imenu datoteke — potrjen poln naziv, glej §1.1, §1.5). Vse odločitve so vključene v ustrezne razdelke zgoraj. Ni preostalih odprtih vprašanj, ki bi blokirala fazo 1.

---

## Priloga B — Dnevnik dopolnil (za sledljivost)

| # | Sprememba | Sprožil |
|---|---|---|
| 1 | `AuditLogEntry` vrnjen v fazo 1 (minimalen obseg) | Potreba dispečerja §2.5 po razlagi ročnih popravkov |
| 2 | `LeaseItem` — razlog obvezen ob ročni spremembi zneska (admin mehanizem) | Isto |
| 3 | Januarski postopek piše neposredno v `LeaseItem.znesek_neto_mesecno` | Definicija indeksacijskega toka |
| 4 | `LeaseItem.datum_prenehanja_postavke`, odstranitev `aktivna` (boolean) | Robni primer — delna prekinitev postavke (potrjeno realno) |
| 5 | `LeaseItem.datum_zacetka_postavke` | Simetrija s #4 (potrjeno realno) |
| 6 | `Invoice.razlog` (za STORNO/DOBROPIS) | Pravila izračuna — razlog na nivoju dokumenta |
| 7 | `core.pravila.zaokrozi()` kot edina točka zaokroževanja | Pravila izračuna — konsistentnost |
| 8 | `LeaseItem.znesek_neto_mesecno` — striktna pozitivnost | Robni primeri (potrjeno trajno pravilo) |
| 9 | `RacunKandidat` — ločitev `Blokada` od `Izjema` | Robni primeri — manjkajoč podpisnik |
| 10 | `VatRegime.clean()` — blokada deaktivacije med aktivno uporabo | Robni primeri — šifrant med aktivno uporabo |
| 11 | `Tenant.email` | Validacija partnerjev — vrzel med §6.9 PRD in modelom pogodbe |
| 12 | `Lease.tip_pogoja_indeksacije`, `Lease.prag_spremembe_odstotek` | Indeksacija — protislovje med "izračuna pogoj" in prostim besedilom |
| 13 | `LeaseItem.indeks_ob_osnovi`, `datum_osnove_indeksacije`, `osnovna_najemnina_ob_sklenitvi` | Indeksacija — izhodiščna točka verige |
| 14 | `LeaseItem.clean()` — zahteva izhodiščni indeks, kadar `ima_klavzulo_indeksacije=True` | Vmesni pregled konsistentnosti |
| 15 | `PoracunIndeksacije` model in `zaznaj_zamujen_poracun()` | Delovni vzorec — zamujen vnos indeksa (§4.2) |
| 16 | Reinterpretacija `indeks_ob_osnovi`/`datum_osnove_indeksacije` za migrirane pogodbe | Vprašanje o osnovi ob prehodu (§7.1) |
| 17 | Sintetičen migracijski `Invoice` (`uvozi_migracijsko_stanje()`) | Δ dispečer bi sicer napačno sprožil "prva izdaja" za vseh 48 pogodb (§7.2) |
| 18 | `Company.logotip`, popravek `Company.maticna_stevilka` na točno 10 mest | Končni pregled proti PRD P0-1 in Prilogi A |
| 19 | `Company.ddv_razporeditev_potrjena`, `preveri_predpogoje_podjetja()` — Signatory preverba preseljena sem iz dispečerja | Končni pregled proti PRD P0-37, §16 postavka 5 |
| 20 | `Company.kratica` odstranjen, `core.pravila.slugify_naziv()` dodan | Rešeno PRD odprto vprašanje O2 (poln naziv, ne kratica ali šifra) |

---

## Priloga C — Popravki k drugim dokumentom v1.0

Ta priloga je namenjena neposrednemu prenosu v obstoječe dokumente. Ne opisuje nove funkcionalnosti — samo neskladja, ki jih je razkril končni pregled tega dokumenta proti izvornim štirim.

### C.1 SAD v1.0 — neskladje v strukturi map (pomembno, priporočena takojšnja popravka)

**Najdba:** SAD §2.2 in §6/§7 (sekvenčni prikazi, presek s P0 zahtevami) predstavljata `/domain`, `/data`, `/render`, `/delivery`, `/storage`, `/ui`, `/app` kot **sedem ločenih vrhnjih map** — z dobesednimi potmi kot `/domain/calculation.py`, `/render/eslog.py`, `/delivery/credentials.py`. Ista struktura je dobesedno prekopirana v `AGENTS.md`.

SAD §3 pa eksplicitno pravi nasprotno: "Django apps so naravna meja za `/domain` in `/data` **skupaj**... vsaka aplikacija (`companies`, `catalogs`, `contracts`, `invoicing` …) znotraj sebe loči `models.py` (`/data`) od `services.py` (`/domain`)." To pomeni, da `/domain` in `/data` **nista** vrhnji mapi, ampak konceptualni oznaki za vsebino znotraj vsake Django app mape.

`/render`, `/delivery`, `/storage` neskladja nimajo — SAD §3 jih sam navaja kot lastne Django app (`rendering`, `delivery`, `archive`), kar se ujema z njihovo vrhnjo umestitvijo v §2.2.

**Zakaj to ni malenkost.** Django migracije, admin registracija in `INSTALLED_APPS` so zgrajeni okoli konvencije "en app, ena mapa, `models.py` v njej". Dobesedna vrhnja `/data` mapa z vsemi modeli vseh app-ov skupaj bi zahtevala nestandardne `AppConfig` nastavitve — nekaj, kar se AI orodja (ChatGPT/Codex) po privzetih vzorcih iz učnih podatkov ne bodo trudila narediti pravilno, ker gre proti konvenciji, ki so se je največ učila.

**Ta specifikacija (§1) sledi §3-razlagi** (per-app `models.py`/`services.py`), ne §2.2 dobesednim potem — model tega dokumenta je zgrajen okoli `companies/`, `catalogs/`, `contracts/`, `invoicing/` kot Django app map.

**Predlagan popravek za SAD v1.1 in AGENTS.md:** v §2.2 in povsod, kjer se pojavljajo dobesedne poti `/domain/...`, `/data/...`, zamenjati z jasno navedbo, da sta `/domain` in `/data` **plasti znotraj vsake Django app mape**, ne vrhnji mapi — npr. `companies/services.py` namesto `/domain/companies.py`. `/render`, `/delivery`, `/storage`, `/ui`, `/app` ostanejo, kot so (nesporni).

### C.2 SAD v1.0 — `RecipientContact`, lastništvo modela

**Najdba:** SAD §4 (pregled entitet) `RecipientContact` uvršča pod app `delivery`. PRD-jev prvotni diagram (§6.1) kontakte ("TO/CC/BCC, po kanalu") uvršča kot del podatkov na `Lease`. Ta specifikacija (§1.3) sledi PRD-jevi umestitvi — podatek se vnese ob urejanju pogodbe, ne ob pošiljanju, zato je smiselneje v `contracts`.

**Predlagan popravek za SAD v1.1:** v tabeli §4 premakniti `RecipientContact` iz vrstice `delivery` v vrstico `contracts`. Django FK-ji med app-i delujejo neodvisno od tega, kje je model deklariran, zato to ni funkcionalna sprememba — samo uskladitev dokumentacije z dejansko odločitvijo.

### C.3 PRD v1.0 — eno vprašanje rešeno, en popravek za prenos

Vsa vsebina, uporabljena v tem dokumentu (§6.1–§6.12, §7, §8, §9, P0 seznam, Dodatek A/B), je bila notranje konsistentna. Dve zahtevi (P0-1 logotip, P0-37 potrditev DDV razporeditve) v prejšnjem osnutku tega FSD dokumenta nista bili modelirani — to je bila vrzel v FSD, ne v PRD; PRD ju je ves čas pravilno zahteval. Popravljeno v §1.1 (dopolnili 18, 19).

**Predlagan popravek za PRD v1.1:** vrstica **O2** v tabeli odprtih vprašanj (§17) se zapre — odločitev je poln naziv podjetja, počiščen za datotečni sistem (glej FSD §1.1, §1.5, dopolnilo 20), ne kratica ali šifra. Vrstico se lahko premakne iz "Odprta vprašanja" v "Odločeno" ali preprosto izbriše, glede na konvencijo PRD za zaprta vprašanja.

### C.4 UIUX prototipi v1.0 — brez potrebnih popravkov

Zaslon "Najemna pogodba — vnos" in "Izgled računa — sklic na pogodbo in objekt" sta bila preverjena polje za poljem proti podatkovnemu modelu (§1) — brez neskladij. Zaslon "Arhiv" je izrecno označen kot P1/faza 6, kar se ujema z izpustitvijo `ArchivedFile` iz tega dokumenta.

### C.5 Ocena obsega in vrednosti v1.0 — brez potrebnih popravkov

Dokument je ocena časa/vrednosti, ne vsebuje poslovnih pravil ali podatkovnega modela — pregled ni razkril vsebine, ki bi jo bilo treba uskladiti s tem FSD.

---

*Konec dokumenta. 48 pogodb, 5 podjetij, 4 podatkovni modeli, 20 dopolnil, 2 neskladji med izvornimi dokumenti, 0 odprtih vprašanj, 0 sprememb obsega faze 1 od začetne razprave.*
