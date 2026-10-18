# Funkcionalna specifikacija — Faza 1 (v1.2)

**Aplikacija Marjan — najemniško zaračunavanje**
**Različica:** 1.2 · **Datum:** 16. september 2026 · **Status:** FINAL · **Podlaga:** PRD v1.2 in SAD v1.2
**Nadomešča:** FSD faze 1 v1.1 FINAL. Različica 1.2 nadomešča v1.1, ki ostaja nespremenjena kot prejšnja potrjena različica.
Interni dokument za razvojno ekipo (AI-podprt razvoj, ChatGPT/Codex). Ni za naročnika.

Obseg te specifikacije: `/domain` + `/data` za module `companies`, `catalogs`, `contracts`, `invoicing` (jedro). Cilj faze 1 po SAD-u: poslovna pravila delujejo in so dokazana s testi, brez zagnanega vmesnika. `/render`, `/ui`, `/delivery`, `MonthlyBatch` kot orkestracijski model in priloge k pogodbi so izven obsega tega dokumenta (faze 2–5).

**Meja prvega zagona:** ta FSD ohranja model `Company.ddv_razporeditev_potrjena` in njegove predpogoje faze 1, vendar ne določa čarovnika, vzorčnega PDF-načina ali podatkovnega zapisa potrditve vzorca. Ti sodijo v prihodnji FSD prvega zagona. Končni sistem mora pred produkcijsko izdajo poleg pogojev iz tega dokumenta preveriti tudi veljavno uporabnikovo potrditev vzorca za vsako podjetje, kot določata PRD v1.2 §16.4 in SAD v1.2 §6.4.

**Spremembe v1.2:** `ddv_razporeditev_potrjena` je izrecno uporabnikova potrditev, odstranjene so vse potrjevalne in blokirne vloge računovodje, meja do prihodnjega FSD prvega zagona pa je zapisana tako, da vzorčni račun ne postane izdani dokument in da bo njegova uporabnikova potrditev ločen produkcijski predpogoj. Dodani so natančni pogoji fizičnega izbrisa, arhiviranja in ponovne aktivacije, zaščita pred kaskadnim izbrisom, strukturirani razlogi za zavrnitev, minimalna revizijska sled ter obvezni testi. Atomsko številčenje je usklajeno z Django 5.2 in SQLite: `IMMEDIATE` transakcije, `leto=0` za NP, DB omejitve ter transakcijski test z dejansko konkurenco.

**Zgodovina v1.1:** v1.1 FINAL je nastala skozi dve razpravni fazi: (a) pet vsebinskih sklopov (podatkovni model, pravila izračuna, robni primeri, validacija partnerjev, indeksacija) in (b) končni pregled štirih izvornih dokumentov. Priloga B beleži vseh 20 dopolnil v1.1 kronološko, Priloga C pa zgodovinske uskladitve z drugimi dokumenti.

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
| `rok_placila_dni_privzeto` | IntegerField | default 15 |
| `ddv_razporeditev_potrjena` | BooleanField | default `False` — uporabnikova potrditev razporeditve enot po DDV režimih za to podjetje, PRD v1.2 §16 in §16.4, P0-37/P0-54 (dopolnilo 19) |
| `aktivno` | BooleanField | default `True` |

DB omejitev za davčno številko:

```python
models.UniqueConstraint(
    fields=['davcna_stevilka'],
    condition=models.Q(aktivno=True),
    name='uniq_active_company_davcna_stevilka',
)
```

`clean()` ostane za razumljivo napako. Ponovna aktivacija se zavrne, če bi omejitev kršila.

**IBAN.** Ob vnosu se odstranijo presledki in vrednost pretvori v velike črke; shrani se kanonična oblika brez presledkov. Validator preveri dovoljene znake, strukturo, predpisano dolžino države ter MOD-97 kontrolno številko. Država ni omejena na `SI`.

*Zakaj `ddv_razporeditev_potrjena` na `Company`, ne kot ločen model v fazi 1.* PRD v1.2 (P0-37/P0-54, §16.4) zahteva uporabnikovo potrditev DDV-nastavitev pred prvo resnično izdajo. V obsegu faze 1 je to stanje predpogoja na podjetju; podrobnejši zapis časa potrditve, pravila samodejne zastarelosti in povezava z ločeno potrditvijo vzorca se določijo v prihodnjem FSD prvega zagona. Računovodja pri tej potrditvi ne sodeluje.

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

Na `company` sme biti največ en trenutni podpisnik; trenutni pomeni `velja_do IS NULL`. Pravilo zagotavljata obe plasti:

```python
models.UniqueConstraint(
    fields=['company'],
    condition=models.Q(velja_do__isnull=True),
    name='uniq_current_signatory_per_company',
)
```

DB omejitev prepreči drugega trenutnega podpisnika tudi zunaj obrazcev in ob sočasnem pisanju. `clean()` ostane za zgodnjo, uporabniku prijazno validacijsko napako.

Za pripravo računa mora biti na `datum_izdaje` veljaven natanko en `Signatory`. Nič veljavnih podpisnikov blokira pripravo; več kot eden pomeni nekonsistentno zgodovino in prav tako blokira pripravo, dokler se obdobja veljavnosti ne popravijo. Podpisnika se ne izbira z razreševanjem po najnovejšem `velja_od`.

**NumberSequence**

*Zakaj ločen model, ne polje na `Company`.* Dve neodvisni zaporedji (NP, ST) z različnim obnašanjem resetiranja (§2.4) potrebujeta lasten prostor za `zadnja_stevilka`, ne moreta biti dve polji na `Company`, ker bi to podvojilo logiko za vsak nov tip dokumenta.

| Polje | Tip | Omejitve |
|---|---|---|
| `company` | FK → Company | |
| `tip` | CharField, choices: `NP`/`ST` | |
| `leto` | IntegerField | required, brez `NULL`; `0` pri `NP`, dejansko koledarsko leto pri `ST` |
| `zadnja_stevilka` | IntegerField | default `0` |

DB omejitve:

```python
models.UniqueConstraint(
    fields=['company', 'tip', 'leto'],
    name='uniq_number_sequence_company_tip_leto',
)
models.CheckConstraint(
    condition=(
        models.Q(tip='NP', leto=0)
        | models.Q(tip='ST', leto__gt=0)
    ),
    name='number_sequence_veljaven_tip_leto',
)
```

Storitev pri `ST` vedno vzame leto iz `datum_izdaje.year`; DB pogoj `leto > 0` je zadnja zaščita pred neveljavno kombinacijo. Pri `NP` je `0` samo interna vrednost in se v številki ne izpiše.

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
| `zahteva_polni_odbitek` | BooleanField | `null=True`, brez privzetega `False`; ob vnosu obvezna izrecna izbira Da/Ne; `NULL` pomeni nerazrešeno staro razvrstitev in blokira uporabo |
| `velja_od` | DateField | required |
| `velja_do` | DateField | `null=True` |
| `eslog_koda` | CharField | optional v fazi 1, polni se v fazi 2 |
| `aktiven` | BooleanField | default `True` |

**Pravilo:** `clean()` prepreči `aktiven=False`, dokler obstaja aktivna `LeaseItem` s to referenco (dopolnilo 10). *Zakaj:* napako je ceneje preprečiti ob viru (en klik v adminu) kot loviti mesec dni kasneje kot izjemo v paketnem pregledu.

**T1 (potrjeno 2026-10-09; PRD §6.3/P0-9).** Zahteva polnega odbitka je neodvisna od `nacin_obracuna` in `zahteva_referenco`. `OBDAVCENO_45` mora imeti `True`; nepravilna nastavitev se zavrne. `OBDAVCENO_STANDARD=False` ohrani obdavčitev parkirišč, garaž in trajno instalirane opreme tudi za prejemnike brez polnega odbitka. Iz drugih režimov se davčnih pogojev ne sklepa.

**Varna migracija.** `catalogs.0002` doda nullable polje brez backfilla; `0003` preveri šifro, način `OBDAVCENO`, stopnjo `22.00`, skupino `OBD_22` in zahtevo reference (`True` za 45, `False` za STANDARD). Samo ti preverjeni vrstici samodejno razvrsti. Če obstajajo nerazrešene vrstice, tudi neaktivne, migracija pred spremembo katerekoli razvrstitve ustavi izvajanje ter navede `id:šifra`. Izrecna uporabnikova preslikava po stopnji 0002 omogoči nadaljevanje; kopiranje privzetega `False` ni dovoljeno. Spremenjene/preimenovane vrstice in preostali režimi zahtevajo odločitev. Končna odstranitev nullable stanja ni del T1.

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
| `status` | CharField, choices: `AKTIVNA`/`ODPOVEDANA`/`PRENEHALA` | default `AKTIVNA`; pogodbeni status, ne stanje arhiviranja |
| `arhivirana` | BooleanField | default `False` — ločeno od pogodbenega statusa; arhivirana pogodba se ne ponuja za nove račune |
| `rok_placila_dni` | IntegerField | default iz `Company.rok_placila_dni_privzeto` |
| `ima_klavzulo_indeksacije` | BooleanField | default `False` |
| `opis_pogoja_indeksacije` | TextField | dokumentarno besedilo za človeka, ni vhod v izračun |
| `tip_pogoja_indeksacije` | CharField, choices: `VEDNO`/`PRAG_SPREMEMBE`/`SAMO_NAVZGOR` | required če `ima_klavzulo_indeksacije=True` |
| `prag_spremembe_odstotek` | DecimalField(5,2) | required če `tip_pogoja_indeksacije='PRAG_SPREMEMBE'` |

**LeaseItem**

*Zakaj `aktivna` (boolean) ni v modelu.* Prvotno je bilo — odstranjeno, ker bi dva neodvisna vira resnice (boolean in datum) prej ali slej razpadla narazen. Stanje »ali se postavka zdaj zaračunava« je izpeljano iz datumov, ne shranjeno. Ločeno polje `arhivirana` spodaj ne opisuje veljavnosti za obračun, temveč uporabnikovo odločitev, da se zapis ne ponuja pri pripravi novih računov.

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
| `arhivirana` | BooleanField | default `False` — ne nadomešča datumov veljavnosti; arhivirana postavka se ne ponuja za nove račune |
| `indeks_ob_osnovi` | DecimalField(6,3) | required če `lease.ima_klavzulo_indeksacije=True` — glej §4 za natančen pomen (referenčna točka, ne nujno sklenitev) |
| `datum_osnove_indeksacije` | DateField | required skupaj z zgornjim |
| `osnovna_najemnina_ob_sklenitvi` | DecimalField(10,2) | informativno/revizijsko, ni vhod v izračun |

Pravilo (`clean()`): kadar `lease.ima_klavzulo_indeksacije=True`, `indeks_ob_osnovi` in `datum_osnove_indeksacije` ne smeta biti prazna — prepreči se ob shranjevanju (dopolnilo 14).

**Upravičenost prejemnika T1.** `LeaseItem.clean()` pokliče skupno domensko pravilo. Pri `zahteva_polni_odbitek=True` morajo hkrati veljati `Tenant.tip=PRAVNA_OSEBA`, `identificiran_za_ddv=True` in `ddv_zavezanec_s_polnim_odbitkom=True`. Napaka navede najemnika, postavko in režim. Isti validator se uporabi ob spremembi najemnika pogodbe za njene nearhivirane postavke, pri izračunu in v predhodni kontroli. Sprememba dejanskega statusa najemnika se ne prepove; nezdružljiva prihodnja uporaba se blokira.

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
| `prejemnik_polni_odbitek` | BooleanField, `null=True`, `editable=False` | **posnetek** statusa prejemnika; stari neznani podatki se ne dopolnijo iz živega najemnika |
| `izdajatelj_naziv` / `_naslov` / `_davcna_stevilka` / `_maticna_stevilka` / `_trr` | CharField ×5 | **posnetek** |
| `izdajatelj_podpisnik_ime` / `_funkcija` / `_slika_pot` | CharField ×3 | **posnetek** veljavne verzije `Signatory` |
| `oznaka_pogodbe` | CharField | `null=True` pri `ST` |
| `objekt_naziv` | CharField | `null=True` |
| `status` | CharField, choices: `OSNUTEK`/`ZA_PONOVNI_PREGLED`/`IZBRAN_ZA_POSILJANJE`/`POSLAN`/`NAPAKA`/`REZULTAT_NI_ZNAN`/`ARHIVIRAN_NI_IZDAN`/`STORNIRAN`/`POPRAVLJEN` | default `OSNUTEK` — v fazi 1 so relevantni `OSNUTEK`, `IZBRAN_ZA_POSILJANJE` in `ARHIVIRAN_NI_IZDAN` (slednji za migracijske zapise, §7) |
| `povezan_racun` | FK → self | `null=True` — za STORNO/DOBROPIS/POPRAVLJEN |
| `razlog` | TextField | required kadar `document_type` ∈ {STORNO, DOBROPIS} |
| `razlog_arhiviranja` | TextField | `null=True`; standardna vrednost `"Migracijski zapis – uvoz stanja pred prehodom na aplikacijo"` za sintetične zapise (§7) |
| `osnova_skupaj` / `ddv_skupaj` / `skupaj_z_ddv` | DecimalField(10,2) ×3 | izračunano ob potrditvi |
| `rendered_pdf_path` / `rendered_eslog_path` / `checksum` | CharField ×3 | polni se v fazi 2 |

DB omejitev:

```python
models.UniqueConstraint(
    fields=['company', 'stevilka'],
    name='uniq_invoice_company_stevilka',
)
```

Ista številka je lahko pri drugem podjetju, več osnutkov z `stevilka=NULL` pa ostane dovoljenih.

**Nespremenljivost po potrditvi.** Ko ima `Invoice.stevilka` vrednost, domenske storitve zavrnejo spremembo vseh poslovnih in posnetnih polj `Invoice`, dodajanje, spremembo ali izbris `InvoiceLine` ter izbris originalnega računa. Dovoljeni so samo predpisani prehodi `status`, nastavitev render poti oziroma `checksum` prek namenskih storitev in zapis evidence pošiljanja. Popravki nastanejo izključno s storno/dobropisom. DB triggerji se ne uporabljajo.

**InvoiceLine**

| Polje | Tip | Omejitve |
|---|---|---|
| `invoice` | FK → Invoice | |
| `lease` | FK → Lease | `null=True` — **samo sledljivost**, ni vir vrednosti |
| `lease_item` | FK → LeaseItem | `null=True`, `on_delete=PROTECT` — **samo sledljivost**, ni vir vrednosti |
| `opis` | TextField | required, sestavljen niz (PRD §6.11), shranjen dobesedno |
| `referenca_listine` | CharField, `blank=True`, default `""` | **posnetek** reference samostojne ST postavke; obvezna, kadar izbrani DDV režim zahteva referenco |
| `datum_reference` | DateField, `null=True`, `blank=True` | **posnetek** datuma reference samostojne ST postavke; obvezen skupaj z zahtevano referenco |
| `osnova` | DecimalField(10,2) | **posnetek** |
| `ddv_stopnja` | DecimalField(5,2) | **posnetek** |
| `ddv_znesek` | DecimalField(10,2) | |
| `skupaj` | DecimalField(10,2) | |
| `ddv_nacin_obracuna` | CharField | **posnetek** |
| `ddv_rezim_sifra` | CharField, `editable=False` | **posnetek** šifre; zgodovinsko neznana vrednost ostane prazna |
| `ddv_zahteva_polni_odbitek` | BooleanField, `null=True`, `editable=False` | **posnetek** zahteve režima; zgodovinsko neznana vrednost ostane `NULL` |
| `ddv_klavzula` | TextField | **posnetek** |
| `ddv_rekap_skupina` | CharField | **posnetek** |
| `je_sorazmerno` | BooleanField | default `False`; `True` pri sorazmernem obračunu (§2.2) |
| `razlog_rocnega_popravka` | TextField | `null=True`, required če je vrednost povožena |
| `izvirno_izracunana_vrednost` | DecimalField(10,2) | `null=True` |

`lease_item` ne nadomešča posnetnih polj: zgodovinske vrednosti vrstice se vedno berejo iz `opis`, `osnova`, `ddv_*` in drugih posnetkov na `InvoiceLine`. Vsaka vrstica, ki izvira iz `LeaseItem`, mora referenco nastaviti; to vključuje NP in poračun indeksacije. Ročna ST vrstica brez izvora v `LeaseItem` sme imeti `lease_item=NULL`.

**Samostojne ST postavke (T8-A).** Kadar izbrani DDV režim zahteva referenco listine, priprava osnutka preveri neprazno referenco in njen datum po enakem pravilu kot `LeaseItem` ter oba shrani na `InvoiceLine`. Poznejše spremembe šifranta teh posnetkov ne spremenijo; zgodovinsko neznane vrednosti ostanejo prazne oziroma `NULL`.

Do namenskega urejanja in potrjevanja v T8-B splošne storitve `spremeni_podatke_racuna`, `spremeni_vrstico`, `dodaj_vrstico` in `izbrisi_vrstico` zavrnejo spremembe osnutkov tipa `ST`. Splošna sprememba tipa dokumenta v `ST` je prav tako zavrnjena. Preverjanje uporablja svež zapis iz baze in poteka pred spremembami v isti atomski transakciji.

**Dokazi pred izdajo T1.** Izračun različice 4 hrani `tenant.full_deduction`, vrstica pa `vat_regime_code` in `requires_full_deduction`. Vsi so del prstnega odtisa. Zamrznitev izbranega kandidata in njegova materializacija preverita izrecne boolean vrednosti ter upravičenost pred dodelitvijo številke; manjkajoči ali napačno tipizirani dokazi se zavrnejo. Neposredna pot potrditve NP preveri tako trenutni izvor kot shranjene dokaze osnutka. Novi dokazi se kopirajo v račun in vrstice; domenske storitve jih ne dovolijo ročno spreminjati. Storno kopira dokaze izvirnika, vključno z zgodovinskim neznanim stanjem.

**Stari paketi.** Različica 3 brez dokazov ne dovoljuje nove izdaje ali porabe številke. Dovoljena je samo natančno preverjena ponovitev že oštevilčenega računa brez novih zapisov ali sprememb zgodovine. Posnetki se ne dopolnijo s trenutnimi matičnimi podatki. Postopek obnove starega neizdanega paketa v koraku 5 zahteva ločeno potrditev uporabnika; T1 ga ne uvaja. Sprememba zahteve polnega odbitka spremeni prstni odtis DDV-nastavitev različice 2 in s tem zastarelost uporabnikovih potrditev.

### 1.5 `core`

**AuditLogEntry**

*Zakaj generično, ne poseben model za vsak primer.* Vstopna točka je bila ena: dispečer sprožilca "znesek se razlikuje" (§2.5) mora znati pojasniti ročni popravek zneska, ki se je zgodil mimo indeksacije ali sorazmernega obračuna. Isti mehanizem se je nato izkazal za dovolj splošnega za vse ročne popravke na `LeaseItem` (dopolnila 2, 4, 5) in za razveljavitev indeksacije (§4).

*Obseg v fazi 1 je namenoma ozek — pojasnilo obveznosti.* PRD P0-26 zahteva revizijsko sled »za vsako spremembo«, kar je splošneje od trenutnega obsega. Faza 1 pokriva spremembe `LeaseItem.znesek_neto_mesecno`, `datum_zacetka_postavke`, `datum_prenehanja_postavke` in minimalni dogodek fizičnega izbrisa iz P0-55. SAD §3 app `audit` isti model pozneje razširi na vse zahtevane modele; dodatek dogodka izbrisa ne pomeni, da faza 1 že izvaja celotno revizijsko sledenje končnega izdelka.

| Polje | Tip | Omejitve |
|---|---|---|
| `content_type` | FK → ContentType | Django `contenttypes` |
| `object_id` | PositiveIntegerField | pri fizičnem izbrisu ostane identifikacijska sled; ni zaščitni FK do izbrisanega zapisa |
| `dejanje` | CharField, choices: `SPREMEMBA`/`FIZICNI_IZBRIS` | default `SPREMEMBA` |
| `polje` | CharField, `blank=True` | ime spremenjenega polja; pri `FIZICNI_IZBRIS` prazen niz |
| `stara_vrednost` | CharField, `blank=True` | stringificirano pri spremembi; pri `FIZICNI_IZBRIS` prazen niz, brez poslovnega posnetka |
| `nova_vrednost` | CharField, `blank=True` | stringificirano pri spremembi; pri `FIZICNI_IZBRIS` prazen niz |
| `razlog` | TextField | required; pri izbrisu standardni zapis, da je uporabnik izrecno potrdil fizični izbris neuporabljenega zapisa |
| `created_at` | DateTimeField | `auto_now_add=True` |

Indexa: `(content_type, object_id, polje, created_at)` za obstoječe poizvedbe sprememb in `(content_type, object_id, dejanje, created_at)` za dogodke življenjskega cikla.

Pri fizičnem izbrisu `content_type`, `object_id`, `dejanje` in `created_at` zagotovijo zahtevani minimum: vrsto zapisa, njegovo identifikacijsko sled, dejanje uporabnika in čas. Generična povezava po izbrisu namenoma ne razreši več aktivne entitete. `polje`, `stara_vrednost` in `nova_vrednost` ostanejo prazni, zato revizijska sled ne hrani celotne poslovne vsebine izbrisanega zapisa.

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

### 1.6 Fizični izbris, arhiviranje in ponovna aktivacija

Pravila tega razdelka veljajo za začetne poslovne podatke: `Company`, `Property`, `Unit`, `Tenant`, `Lease` in `LeaseItem`. Enaka varovalka se uporabi tudi za povezane šifrante ali nastavitvene zapise, kadar aplikacija zanje ponuja fizični izbris.

#### Odločitev o fizičnem izbrisu

Domenska storitev appa, ki je lastnik zapisa, mora pred izbrisom vrniti strukturirano odločitev:

```text
IzbrisOdlocitev(
    dovoljeno: bool,
    razlogi: seznam[IzbrisRazlog],
    mozno_arhiviranje: bool,
)

IzbrisRazlog(
    koda: str,
    sporocilo: str,
    tip_povezave: str,
    stevilo_povezav: int,
)
```

Fizični izbris je dovoljen samo, ko so hkrati izpolnjeni vsi pogoji:

1. zapis še nikoli ni bil poslovno uporabljen; sam tehnični zapis njegovega nastanka ne šteje kot uporaba;
2. ne obstaja povezava z osnutkom ali izdanim `Invoice` oziroma `InvoiceLine`;
3. ne obstaja povezava s pogodbo, pogodbeno postavko, nepremičnino, enoto, najemnikom ali drugim nadrejenim oziroma podrejenim poslovnim zapisom;
4. ne obstaja povezava s PDF-jem, prilogo ali drugim ustvarjenim dokumentom;
5. ne obstaja povezava z `NumberSequence`;
6. ne obstaja povezava z `DeliveryAttempt` ali drugo evidenco pošiljanja;
7. ne obstaja uporabnikova potrditev vzorca ali DDV-nastavitev;
8. ne obstaja drug poslovno pomemben revizijski ali zgodovinski dogodek;
9. uporabnik je po jasnem opozorilu podal izrecno potrditev fizičnega izbrisa.

Koda razloga mora biti stabilna in primerna za teste. Najmanjši nabor je: `ZAPIS_UPORABLJEN`, `POVEZAN_RACUN`, `POSLOVNA_POVEZAVA`, `PODREJENI_ZAPISI`, `POVEZAN_DOKUMENT`, `POVEZANO_STEVILCENJE`, `POVEZANO_POSILJANJE`, `POVEZANA_POTRDITEV`, `POSLOVNA_ZGODOVINA` in `POTRDITEV_MANJKA`. Ena odločitev lahko vsebuje več razlogov hkrati.

Vsi poslovni tuji ključi, ki bi lahko povzročili odstranitev začetnega ali zgodovinskega zapisa, uporabljajo `on_delete=PROTECT` oziroma enakovredno zaščito. `CASCADE` ni dovoljen za poslovno drevo iz tega razdelka. Če obstaja katerikoli podrejeni zapis, je izbris starša zavrnjen; aplikacija podrejenih zapisov ne izbriše samodejno.

Dovoljeni izbris teče v eni kratki `transaction.atomic()` transakciji, ki zaradi SQLite `transaction_mode='IMMEDIATE'` pridobi zaklep za pisanje ob začetku: storitev ponovno preveri vse reference, zapiše minimalni `AuditLogEntry(dejanje="FIZICNI_IZBRIS")` in šele nato izbriše poslovni zapis. Če med preverjanjem nastane nova povezava ali zaščitna omejitev zavrne izbris, se transakcija povrne in storitev vrne strukturiran razlog namesto delnega rezultata. `select_for_update()` se ne uporablja.

#### Arhiviranje

Če fizični izbris ni dovoljen, je `mozno_arhiviranje=True` in UI ponudi arhiviranje oziroma mehki izbris. Za `Company`, `Property`, `Unit` in `Tenant` se uporabi obstoječe polje `aktivno`/`aktivna`/`aktiven=False`; `Lease` in `LeaseItem` uporabljata ločeno polje `arhivirana=True`, ker pogodbeni status in datumi veljavnosti niso stanje arhiva.

Arhiviranje:

- ne izbriše zapisa ali njegovih povezav;
- ne spremeni izdanih računov, osnutkov ali zgodovinskih dokumentov;
- izključi zapis iz izbir in iz priprave novih računov; kandidat izpade, če je arhiviran katerikoli člen verige podjetje → nepremičnina → enota → najemnik → pogodba → postavka;
- ne arhivira ali briše podrejenih zapisov kaskadno;
- se zabeleži kot običajna sprememba v revizijski sledi.

#### Ponovna aktivacija

Ponovna aktivacija je dovoljena samo, če zapis obstaja in je arhiviran, so vsi njegovi obvezni nadrejeni zapisi aktivni, ne nastane kršitev enoličnosti ali referenčne celovitosti ter zapis uspešno prestane trenutno veljavne domenske validacije. Ponovna aktivacija starša ne aktivira samodejno njegovih podrejenih zapisov. Če kateri pogoj ni izpolnjen, storitev vrne strukturirane razloge in zapis ostane arhiviran. Uspešna ponovna aktivacija se zabeleži v revizijski sledi.

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

**Vhod običajne ST fakture.** Bruto znesek mora biti končen `Decimal`, večji od nič, z največ dvema decimalnima mestoma in v obsegu denarnega polja računa. Ničelni in negativni bruto zneski se zavrnejo; vhod se ne zaokroži tiho.

**Zakaj DDV ni izračunan neodvisno.** Neodvisno zaokroževanje osnove in DDV lahko odstopa za cent od vnesenega bruto zneska — nesprejemljivo na davčnem dokumentu. DDV je definiran kot preostanek, ne kot ločen izračun, s čimer je vsota zajamčeno natančna.

```python
def izracunaj_iz_bruto(bruto, stopnja):
    osnova = zaokrozi(bruto / (1 + stopnja / Decimal(100)))
    ddv = bruto - osnova
    return osnova, ddv
```

Uporabljeno v ročnih storitvenih fakturah (faza 5 kot celoten tok; funkcija je obvezen test faze 1).

### 2.4 Številčenje in atomarnost

Projekt uporablja Django 5.2 in v nastavitvah SQLite določi `DATABASES['default']['OPTIONS']['transaction_mode'] = 'IMMEDIATE'`. Zunanji `transaction.atomic()` zato pridobi SQLite zaklep za pisanje že ob začetku. Transakcija vsebuje samo povečanje zaporedja, nastanek nespremenljivega posnetka računa in prehod statusa; generiranje PDF/XML, datotečni zapisi ter pošiljanje ostanejo zunaj nje. `select_for_update()` se ne uporablja, ker na SQLite nima učinka.

**Zakaj cela dodelitev in status v eni transakciji.** Če transakcija propade, se števec povrne — nobena številka ni "zažgana". Če uspe, je številka trajno porabljena, tudi če poznejši koraki (PDF) spodletijo — ponovni poskus uporabi isto številko, ne kliče funkcije znova.

```python
with transaction.atomic():
    leto = datum_izdaje.year if tip == 'ST' else 0
    seq, _ = NumberSequence.objects.get_or_create(
        company=company, tip=tip, leto=leto, defaults={'zadnja_stevilka': 0}
    )
    seq.zadnja_stevilka = F('zadnja_stevilka') + 1
    seq.save(update_fields=['zadnja_stevilka'])
    seq.refresh_from_db(fields=['zadnja_stevilka'])
    invoice.stevilka = format_stevilka(tip, seq.zadnja_stevilka, leto)
    invoice.status = 'IZBRAN_ZA_POSILJANJE'
    invoice.save()
```

`STORNO`/`DOBROPIS` gresta skozi isto funkcijo in isti `tip` kot izvirnik — ni ločenega števca. Enolična in kontrolna DB omejitev preprečita več zapisov NP za isto podjetje ter neveljavne kombinacije tipa in leta.

#### T8-C: domenska potrditev samostojnega ST osnutka

`invoicing.services` izpostavlja `fingerprint_osnutka_st`, `preglej_osnutek_st` in
`potrdi_osnutek_st`; implementacija je v `invoicing/st_confirmation.py`.
Posamični pregled zahteva izrecen `individual_review=True` in pričakovani fingerprint
pregledanega shranjenega osnutka. Dokazilo je nespremenljiv `AuditLogEntry`, vezan na
konkretni račun, vse shranjene postavke, materialne vire, podpisnika, preverjene assete,
verzijo predloge in privzeti plačilni rok podjetja. Sam ogled strani T8-B dokazila ne ustvari.

Priprava ST osnutka v isti transakciji shrani tudi nespremenljivo revizijsko dokazilo
DDV vira (`st_vat_source_v1`). S tem zazna spremembe pogojev, ki niso samostojna polja
postavke, zlasti zahteve reference in veljavnosti režima. Starejši osnutek brez tega
dokazila se ne potrdi; potreben je nov osnutek. Dokazila se ne dopolnjuje iz trenutnega
šifranta za nazaj.

Potrditev sprejme samo shranjen `ST/OSNUTEK`, brez številke, pogodbe in korekcijske
povezave. Ponovno preveri posnetke izdajatelja, prejemnika, DDV in postavk, obvezno
referenco z datumom, upravičenost, obdobje ter točen izračun iz pozitivnega bruto zneska.
Ob neskladju z materialnimi viri zavrne potrditev; zgodovinskih posnetkov ne osvežuje.
Vsote določi iz preverjenih postavk, rok plačila pa iz datuma izdaje in
`Company.rok_placila_dni_privzeto`; neskladne že shranjene vsote ali rok zavrne.

Logotip in podpis preveri ter zamrzne pred transakcijo. Storitvi pregleda in potrditve
morata začeti zunaj obstoječe transakcije. V kratki transakciji `IMMEDIATE` ponovno
prebere stanje, preveri dokazilo pregleda in vse produkcijske predpogoje, pokliče
obstoječe ST številčenje ter shrani številko, nespremenljive reference in status
`IZBRAN_ZA_POSILJANJE`. Preverjanje dostopnosti assetov pri tem uporablja predhodno
preverjene reference; v transakciji ni datotečnih ali zunanjih operacij.
Napaka pred potrditvijo transakcije povrne račun in števec. Vsak ponovljen ali sočasen
drugi poskus je izrecno zavrnjen; ne vrne nove številke in ne prepiše potrjenega računa.

**Meja domenske potrditve:** ta storitev ne pripravlja PDF, ne pošilja in ne arhivira.
Uradni PDF in operativna izdaja sta dodana v T8-D/E1; ločena dostava je opisana spodaj.
`FEATURE_ESLOG` ostaja izključen; ST ne razrešuje konfiguracije eSLOG, vezane na pogodbo.
Ob neuspešni potrditvi lahko ostanejo neuporabljene vsebinsko naslovljene kopije
assetov, pripravljene pred transakcijo; račun in števec ostaneta nespremenjena.

#### T8-E2: operativna dostava samostojnega ST

Priprava sprejme samo shranjen, oštevilčen samostojni `ST`, brez pogodbe in korekcijske
reference, v stanju `IZBRAN_ZA_POSILJANJE`, ki še nima poskusa dostave oziroma ima
zadnji poskus preklican po spodnjem pravilu T8-E3. Zahteva že
registriran uradni PDF s preverjeno kontrolno vsoto SHA-256. Pregled, priprava in
pošiljanje ne generirajo ali popravljajo PDF ter ne spreminjajo številke ali finančnih
posnetkov. Ob manjkajočem ali poškodovanem PDF je dostava blokirana.

Uporabnik izrecno vnese TO in neobvezni CC. `Tenant.email` povezanega prejemnika je
samo predlog v obrazcu; pri ročnem prejemniku brez `Tenant` je TO prazen. Kontakti
`RecipientContact` se pri samostojnem ST ne uporabljajo. Vnos ne ustvarja ali spreminja
najemnikov. Naslovi se preverijo in obrežejo, domena se normalizira, ponovitve v isti
vlogi se odstranijo. Ponavljanje naslova med TO, CC in BCC se zavrne za izrecen popravek,
da skriti prejemnik ne postane vidni prejemnik. BCC vsebuje nastavljeni naslov
računovodje izdajatelja; naslov BCC je samo v ovojnici SMTP, nikoli v glavi sporočila.

Pred nastankom `DeliveryAttempt` zaslon prikaže končni pregled pošiljatelja, TO, CC in
BCC. Podpisan pregled je vezan na račun, podjetje, številko, prejemnike in PDF.
Ločeni POST z obvezno potrditvijo pripravi nespremenljivi poskus. Spremenjene
nastavitve ali metapodatki zahtevajo nov pregled. GET ne pripravlja, pošilja ali obnavlja
poskusov. Priprava ni pošiljanje; sledi ločen POST »Pošlji PDF«.

Pošiljanje uporablja obstoječi `send_pdf_email`, konfiguracijo izdajatelja, Keychain,
zaklep izvajanja in prehode `PREPARED`, `SUBMISSION_STARTED` ter končni rezultat.
Pred zunanjimi operacijami se preverita zadnji poskus in ujemanje uporabnika SMTP
ter trenutnega pošiljatelja izdajatelja z zamrznjenim pošiljateljem. PDF se ponovno
preveri proti zamrznjenim metapodatkom. SMTP in Keychain potekata zunaj transakcije;
operativni pogledi izključijo tudi `ATOMIC_REQUESTS`.

Potrjen sprejem SMTP uporabi obstoječi prehod v `POSLAN`; potrjena napaka v `NAPAKA`.
Samo zadnja potrjena napaka omogoča obstoječo varno pripravo ponovitve z istimi
prejemniki, pošiljateljem in PDF. Ponovitev zahteva ločeno dejanje pošiljanja.
`UNKNOWN` blokira samodejno ponovno oddajo. Uporabnik preveri mapo Poslano ali drugo
zanesljivo dokazilo in z obveznim razlogom izrecno potrdi »poslano« oziroma »ni poslano«.
Obstoječi storitvi ohranita izvirni poskus in zapišeta nespremenljivo revizijsko
dokazilo; druga možnost samo pripravi varen ponovni poskus. Pri prekinjeni oddaji
izrecni POST sproži obstoječo obnovo: aktivni zaklep ostane zaščiten, prekinjen poskus
brez končnega rezultata postane `UNKNOWN`. Čas sam ne dovoljuje ponovitve.

#### T8-E3: varen preklic in zamenjava pripravljenega poskusa ST

Potrjena poslovna odločitev dovoljuje preklic samo zadnjega poskusa `PREPARED`,
brez `submission_started_at` in identitete izvajanja. Račun mora pripadati zahtevanemu
podjetju ter ostati oštevilčen samostojni ST v stanju `IZBRAN_ZA_POSILJANJE`.
Ločeni POST zahteva obvezen neprazen razlog in izrecno potrditev preklica.

Storitev pred branjem in spreminjanjem stanja pridobi isti `delivery_execution_lock`
kot pošiljanje. Kratka transakcija SQLite `IMMEDIATE` ponovno preveri račun in zadnji
poskus ter s pogojnim prehodom `PREPARED` v `CANCELLED` spremeni samo rezultat in
čas zaključka. `DeliveryAttempt.save()` se za spremembo ne uporablja. V isti transakciji
nastane nespremenljiv `AuditLogEntry` s prejšnjim in novim rezultatom, razlogom,
časom, identiteto podjetja, računa in poskusa ter zamrznjenimi prejemniki, pošiljateljem
in metapodatki PDF. Napaka revizijskega zapisa razveljavi tudi preklic. Sočasni operaciji
pošiljanja in preklica imata samo enega zmagovalca. Zaklep se pridobi zunaj transakcije;
operativni pogled izključi `ATOMIC_REQUESTS`.

`CANCELLED` je trajen in ni potrjena napaka. Nobena storitev ali UI ga ne sme poslati,
izbrisati, ponovno uporabiti ali spremeniti njegovih zamrznjenih podatkov. UI prikaže
opozorilo, obvezni razlog, potrditev in ločeno zgodovino vseh poskusov. Preklic nikoli
samodejno ne pripravi zamenjave. Nov izrecen pregled TO/CC ohrani pravilo BCC računovodji
in preverjanje uradnega PDF. Podpisano dokazilo pregleda vsebuje tudi ID, zaporedje in
rezultat zadnjega poskusa oziroma odsotnost poskusa. Priprava v kratki transakciji ponovno
preveri celoten pregled, zato stari tokeni in ponovljeni POST ne ustvarijo zamenjave.
Nov poskus ima drug ID in zaporedje prejšnjega + 1; prvi poskus ostaja 1.

Preklic po začetku oddaje ali pri spremenjenem stanju računa ni dovoljen. `ACCEPTED`,
`CONFIRMED_FAILURE`, `UNKNOWN` in že preklicani poskusi niso upravičeni. Obnova po
prekinitvi SMTP, razrešitev `UNKNOWN` in obstoječe ponovitve z zamrznjenimi prejemniki
ostanejo nespremenjene. Preklic ne spreminja računa, vrstic, številčenja, datuma izdaje,
uradnega PDF ali njegove kontrolne vsote. Pravilo ne velja za mesečni NP.

**T8-F:** sprejem SMTP ni dokaz arhiviranja. Dostava ST ne izvaja arhiviranja ali
varnostnih kopij in ni vključena v mesečni NP cikel. Preostane ločena integracija
preverjenega lokalnega in iCloud arhiva ter varnostnih kopij po obstoječih pravilih,
z dokazi o celovitosti in varnim nadaljevanjem ob delnem neuspehu.

### 2.5 Δ zaznava — dispečer in predpogoji podjetja

**Zakaj sta dva ločena mehanizma, ne eden.** Manjkajoč podpisnik in nepotrjena razporeditev DDV režimov nista lastnost ene pogodbe — sta lastnost podjetja. Preverjati ju za vsako od 15 pogodb posebej bi bilo počasno in zavajajoče (15× ista napaka). Zato: en predpogojni pregled na podjetje (korak 2, PRD §7.4), en dispečer na pogodbo (korak 3).

**Predpogoji podjetja** (korak 2 — "predhodna kontrola"):

```python
def preveri_predpogoje_podjetja(company, datum_izdaje) -> list[Blokada]:
    blokade = []
    if not company.ddv_razporeditev_potrjena:
        blokade.append(Blokada("DDV razporeditev enot še ni potrjena — Nastavitve"))
    podpisniki = Signatory.objects.filter(company=company, velja_od__lte=datum_izdaje).filter(
        Q(velja_do__isnull=True) | Q(velja_do__gte=datum_izdaje)
    )
    if not podpisniki.exists():
        blokade.append(Blokada(f"Ni veljavnega podpisnika za {company.naziv} na {datum_izdaje}"))
    elif podpisniki.count() > 1:
        blokade.append(Blokada(f"Več veljavnih podpisnikov za {company.naziv} na {datum_izdaje}; popravite obdobja veljavnosti"))
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

**Razveljavitev** (PRD: "mogoča le, dokler po njej ni bil izdan noben račun") je guard v domenski funkciji, ne novo polje: preveri obstoj `InvoiceLine` z `lease_item=indexation_event.lease_item` po `datum_uskladitve`; vrstice drugih postavk iste pogodbe ne blokirajo razveljavitve. Če taka vrstica obstaja, razveljavitev zavrne. Sama razveljavitev piše `AuditLogEntry` z razlogom "Razveljavljena indeksacija #{id}" in povrne `znesek_neto_mesecno`.

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
        lease_item=lease_item,
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

Vrstica ohrani `lease_item=indexation_event.lease_item`, podeduje njegov `vat_regime` in gre skozi isti DDV izračun kot vsaka druga vrstica (§2.1). Izračunane vrednosti se shranijo v posnetna polja `InvoiceLine`; poznejše spremembe `LeaseItem` jih ne spreminjajo. Več prizadetih mesecev naenkrat (zamuda več kot en mesec) da več ločenih vrstic, vsako s svojim obdobjem.

---

## 5. Robni primeri

**Zakaj so nekateri od teh v ločenem seznamu, ne v pravilih izračuna.** To niso formule, so odločitve o tem, kaj se zgodi na robu — kjer PRD molči ali kjer je bila potrebna presoja, ne le prevod besedila v kodo.

**Šifrant med aktivno uporabo.** Glej §1.2, dopolnilo 10 — blokada deaktivacije, ne runtime odkritje.

**Fizični izbris in sočasna povezava.** Glej §1.6 — začetno preverjanje ni dovolj. Storitev reference ponovno preveri v isti transakciji tik pred izbrisom; zaščitni FK je zadnja obramba pred kaskadnim ali osirotelim rezultatom.

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

**`core/tests/test_zivljenjski_cikel.py`**
| Test | Preveri |
|---|---|
| `test_fizicni_izbris_dovoljen_za_neuporabljen_zapis_brez_referenc` | Izrecno potrjen izbris uspe samo za zapis brez poslovne uporabe in povezav |
| `test_dve_aktivni_podjetji_z_isto_davcno_stevilko_zavrnjeni` | DB omejitev zavrne drugi aktivni zapis |
| `test_arhivirano_podjetje_z_isto_davcno_stevilko_dovoljeno` | Duplikat je dovoljen, kadar je arhiviran |
| `test_reaktivacija_ob_konfliktu_davcne_stevilke_zavrnjena` | Zapis ostane arhiviran |
| `test_fizicni_izbris_brez_izrecne_potrditve_zavrnjen` | Razlog `POTRDITEV_MANJKA`; zapis ostane |
| `test_vsaka_prepovedana_vrsta_reference_blokira_izbris` | Parametrizirano: račun, poslovna povezava, dokument, številčenje, pošiljanje, uporabnikova potrditev in poslovna zgodovina |
| `test_podrejeni_zapis_blokira_starsa_brez_kaskade` | Razlog `PODREJENI_ZAPISI`; starš in otrok ostaneta |
| `test_zavrnjena_odlocitev_vrne_vse_strukturirane_razloge` | Stabilne kode, razumljiva sporočila, vrsta in število povezav; `mozno_arhiviranje=True` |
| `test_arhiviranje_ohrani_reference_in_izkljuci_nove_racune` | Zgodovina ostane, arhivirani člen verige ni ponujen in ne postane nov kandidat |
| `test_ponovna_aktivacija_veljavnega_zapisa_uspe` | Vsi nadrejeni aktivni in vse trenutne validacije uspejo |
| `test_ponovna_aktivacija_z_neveljavnim_nadrejenim_ali_konfliktom_zavrnjena` | Strukturiran razlog; zapis ostane arhiviran |
| `test_ponovna_aktivacija_ne_aktivira_otrok` | Ni kaskadne ponovne aktivacije |
| `test_dovoljeni_izbris_pusti_minimalno_revizijsko_sled` | Ostanejo vrsta/ID, `FIZICNI_IZBRIS` in čas; poslovna vsebina ter aktivna entiteta ne ostaneta |
| `test_socasen_nastanek_reference_varno_prepreci_izbris` | Ponovno preverjanje in `PROTECT` preprečita delni ali osiroteli rezultat |

**`companies/tests/test_stevilcenje.py`**
| Test | Preveri |
|---|---|
| `test_atomarno_dodeljevanje_socasno` | `TransactionTestCase` oziroma `pytest.mark.django_db(transaction=True)` zažene dejanske konkurenčne transakcije prek ločenih DB povezav; noben klic ne dobi podvojene številke |
| `test_np_ne_resetira_cez_leto` | `NP` se nadaljuje čez 31.12./1.1. |
| `test_st_resetira_po_letu_izdaje` | `ST` se resetira glede na `datum_izdaje.year` |
| `test_np_uporablja_leto_nic_in_je_enolicen` | `NP` uporablja `leto=0`; drugi zapis za isto podjetje krši DB enoličnost |
| `test_number_sequence_zavrne_neveljavno_kombinacijo_tipa_in_leta` | DB `CheckConstraint` zavrne `NP` z letom, različnim od `0`, in `ST` z letom `0` |
| `test_podvojena_stevilka_istega_podjetja_zavrnjena` | DB `UniqueConstraint(company, stevilka)` zavrne podvojitev |
| `test_ista_stevilka_drugega_podjetja_dovoljena` | Enoličnost je omejena na podjetje |
| `test_vec_null_stevilk_dovoljenih` | Več osnutkov brez številke je dovoljenih |
| `test_stevilka_dodeljena_sele_ob_potrditvi` | Osnutek nima `stevilka` |
| `test_neuspesna_transakcija_ne_zazge_stevilke` | Propad transakcije povrne `zadnja_stevilka` |
| `test_storno_uporabi_isto_zaporedje` | `STORNO` dobi naslednjo številko istega niza |

**`companies/tests/test_iban.py`**
| Test | Preveri |
|---|---|
| `test_veljaven_iban_sprejet` | Veljaven IBAN prestane validacijo |
| `test_iban_z_napacnim_checksumom_zavrnjen` | MOD-97 napaka blokira shranjevanje |
| `test_iban_z_napacno_dolzino_zavrnjen` | Dolžina ne ustreza državi |
| `test_iban_s_presledki_normaliziran` | Shrani se brez presledkov in z velikimi črkami |

**`companies/tests/test_predpogoji.py`**
| Test | Preveri |
|---|---|
| `test_drugi_trenutni_podpisnik_krsi_db_omejitev` | DB zavrne drugi `Signatory` z `velja_do IS NULL` za isto podjetje |
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
| `test_np_vrstica_iz_leaseitem_hrani_referenco` | `lease_item` je nastavljen |
| `test_st_vrstica_brez_leaseitem_dovoljena` | `lease_item=NULL` |
| `test_sprememba_leaseitem_ne_vpliva_na_izdano_vrstico` | Posnetna polja ostanejo vir zgodovinskih vrednosti |
| `test_neposredna_sprememba_potrjenega_racuna_zavrnjena` | Poslovna/snapshot polja ostanejo nespremenjena |
| `test_neposredna_sprememba_ali_izbris_vrstice_potrjenega_racuna_zavrnjena` | `InvoiceLine` je po potrditvi nespremenljiv |
| `test_izbris_originalnega_potrjenega_racuna_zavrnjen` | Popravek je možen samo s storno/dobropisom |

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
| `test_razveljavitev_blokirana_po_izdanem_racunu` | Blokira samo vrstica istega `lease_item` |
| `test_razveljavitev_ne_blokira_vrstica_druge_postavke` | Drugi `lease_item` iste pogodbe ne blokira |
| `test_razveljavitev_povrne_prejsnjo_vrednost` | |
| `test_datum_uskladitve_privzeto_prvi_februar` | §4 |

**`contracts/tests/test_poracun_indeksacije.py`**
| Test | Preveri |
|---|---|
| `test_zamuja_en_mesec_ena_vrstica_poracuna` | §4.2; zaznava po konkretnem `lease_item` |
| `test_zamuja_vec_mesecev_vec_locenih_vrstic` | Vsaka s svojim obdobjem |
| `test_poracun_ne_uporabi_druge_postavke_iste_pogodbe` | Filter uporablja `lease_item`, ne samo `lease` |
| `test_poracun_vrstica_ohrani_lease_item` | Vrstica referencira izvorni `LeaseItem` |
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
            invoice=invoice, lease=lease, lease_item=item,
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
- **`Company.ddv_razporeditev_potrjena`** — uporabnikova potrditev DDV-nastavitev za podjetje (§1.1, PRD v1.2 §16 in §16.4); računovodja ne pregleduje in ne potrjuje.
- **`VatRegime` šifrant** — privzeta vsebina že v PRD Dodatku A, standardna namestitev.

Vsi štirje spadajo v operativni priročnik (PRD v1.2 §16.3) kot preverljiv seznam pred prvo produkcijsko izdajo — mehanizmi zanje so zdaj definirani v tem dokumentu, izvedba je enkraten ročni korak uporabnika.

Ločena uporabnikova potrditev vzorca za vsako od petih podjetij je dodatni produkcijski predpogoj končnega sistema. Ker vzorčni renderer, čarovnik in zapis te potrditve niso v obsegu faze 1, jih bo opredelil prihodnji FSD prvega zagona; ta dokument jih ne nadomešča.

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
| 19 | `Company.ddv_razporeditev_potrjena`, `preveri_predpogoje_podjetja()` — Signatory preverba preseljena sem iz dispečerja | Končni pregled proti PRD P0-37, §16 postavka 5; v v1.2 pojasnjeno, da potrjuje uporabnik |
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

**Zgodovinski popravek, izveden v SAD v1.1 in ohranjen v SAD v1.2 ter AGENTS.md:** v §2.2 in povsod, kjer se pojavljajo dobesedne poti `/domain/...`, `/data/...`, zamenjati z jasno navedbo, da sta `/domain` in `/data` **plasti znotraj vsake Django app mape**, ne vrhnji mapi — npr. `companies/services.py` namesto `/domain/companies.py`. `/render`, `/delivery`, `/storage`, `/ui`, `/app` ostanejo, kot so (nesporni).

### C.2 SAD v1.0 — `RecipientContact`, lastništvo modela

**Najdba:** SAD §4 (pregled entitet) `RecipientContact` uvršča pod app `delivery`. PRD-jev prvotni diagram (§6.1) kontakte ("TO/CC/BCC, po kanalu") uvršča kot del podatkov na `Lease`. Ta specifikacija (§1.3) sledi PRD-jevi umestitvi — podatek se vnese ob urejanju pogodbe, ne ob pošiljanju, zato je smiselneje v `contracts`.

**Zgodovinski popravek, izveden v SAD v1.1 in ohranjen v SAD v1.2:** v tabeli §4 premakniti `RecipientContact` iz vrstice `delivery` v vrstico `contracts`. Django FK-ji med app-i delujejo neodvisno od tega, kje je model deklariran, zato to ni funkcionalna sprememba — samo uskladitev dokumentacije z dejansko odločitvijo.

### C.3 PRD v1.0 — eno vprašanje rešeno, en popravek za prenos

Vsa vsebina, uporabljena v tem dokumentu (§6.1–§6.12, §7, §8, §9, P0 seznam, Dodatek A/B), je bila notranje konsistentna. Dve zahtevi (P0-1 logotip, P0-37 potrditev DDV razporeditve) v prejšnjem osnutku tega FSD dokumenta nista bili modelirani — to je bila vrzel v FSD, ne v PRD; PRD ju je ves čas pravilno zahteval. Popravljeno v §1.1 (dopolnili 18, 19).

**Zgodovinski popravek, izveden v PRD v1.1 in ohranjen v PRD v1.2:** vrstica **O2** v tabeli odprtih vprašanj (§17) se zapre — odločitev je poln naziv podjetja, počiščen za datotečni sistem (glej FSD §1.1, §1.5, dopolnilo 20), ne kratica ali šifra. Vrstico se lahko premakne iz "Odprta vprašanja" v "Odločeno" ali preprosto izbriše, glede na konvencijo PRD za zaprta vprašanja.

### C.4 UIUX prototipi v1.0 — brez potrebnih popravkov

Zaslon "Najemna pogodba — vnos" in "Izgled računa — sklic na pogodbo in objekt" sta bila preverjena polje za poljem proti podatkovnemu modelu (§1) — brez neskladij. Zaslon "Arhiv" je izrecno označen kot P1/faza 6, kar se ujema z izpustitvijo `ArchivedFile` iz tega dokumenta.

### C.5 Ocena obsega in vrednosti v1.0 — brez potrebnih popravkov

Dokument je ocena časa/vrednosti, ne vsebuje poslovnih pravil ali podatkovnega modela — pregled ni razkril vsebine, ki bi jo bilo treba uskladiti s tem FSD.

---

*Konec dokumenta. 48 pogodb, 5 podjetij, 4 podatkovni modeli, 20 dopolnil, 2 neskladji med izvornimi dokumenti, 0 odprtih vprašanj, 0 sprememb obsega faze 1 od začetne razprave.*
