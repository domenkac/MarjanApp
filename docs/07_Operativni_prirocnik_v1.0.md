# MarjanApp — operativni priročnik v1.0

**Podlaga:** `8d4cff5` — Add verified offline backup restore. **Datum:** 9. oktober 2026.
Za uporabnika, ki upravlja račune, in izvajalca tehnične pomoči. Poslovna pravila določa PRD v1.2, zlasti §11.3 »Varnostna kopija: Dropbox« in §16.3 »Predaja: uporabniška navodila in operativni priročnik«.

**Ob težavi najprej ustavite izdajo in pošiljanje.** Zapišite podjetje, obdobje, številko računa, čas in prikazano napako. Ohranite obstoječe podatke in posnetke. Ne popravljajte baze na roko. Gesel ne vključujte v zapis težave.

Razvojno preverjanje na Windows ni dokaz produkcijskega delovanja na macOS. Obnovo po tem priročniku, Keychain in oblačne mape preverite na ciljnem Macu ob predaji. **P0-31 in P0-44 nista potrjena kot zaključena produkcijska predaja.**

## 1. Varnostne kopije in obnova

### Kaj kopija vsebuje in kaj pomeni oblak

ZIP posnetek vsebuje skladno kopijo baze SQLite (`database/db.sqlite3`), celotni lokalni arhiv (`archive/`), celotno mapo medijskih datotek (`media/`) in opis posnetka (`manifest.json`). Medijske datoteke vključujejo logotipe, podpise in njihove nespremenljive kopije. Datoteke prilog so vključene, **če so shranjene znotraj `MEDIA_ROOT`**. Datoteke zunaj zajetih map niso vključene.

- **Lokalni arhiv:** delovna kopija dokumentov.
- **iCloud arhiv:** druga arhivska lokacija. Replikacija lahko razširi izbris ali okvaro; ni nadomestilo za posnetek.
- **Dropbox:** mapa datiranih ZIP posnetkov. Izbris iz živega arhiva ne spremeni že izdelanega ZIP-a.

ZIP zajema lokalni arhiv, ne dodatne kopije iCloud arhiva. Uspešno preverjanje datoteke v lokalni Dropbox mapi še ne potrjuje njenega prenosa v oblak. Pred uporabo na drugem računalniku preverite, da je celotni ZIP prenesen in lokalno dostopen.

PRD §11.3 zahteva posnetke pred in po mesečnem postopku ter po urniku. Koda sproži kopijo pred novim ciklom in po uspešnem zaključku, ne ob nadaljevanju. Napaka kopiranja ne razveljavi mesečnega stanja; ob opozorilu preverite lokacije in ustvarite kopijo ročno. **Razporejevalnik ni izveden.** Oznaka `SCHEDULED` ne nastavi urnika. Samodejno brisanje in politika hranjenja generacij nista izvedena.

### Ustvarite in preverite posnetek

1. Med kopiranjem ne spreminjajte ročno arhiva ali medijskih datotek.
2. Odprite **Nastavitve → Arhiv**. Preverite tri lokacije. Kliknite **Ustvari varnostno kopijo**.
3. Zahtevajte sporočilo »Varnostna kopija je ustvarjena in preverjena.« Zapišite čas in ime zadnje uspešne kopije.
4. Preverite, da ZIP obstaja v nastavljeni Dropbox mapi. Izvedena imena imajo obliko `LLLL-MM-DD_HHMMSS_backup.zip`.
5. Za dokaz uporabnosti izvedite obnovitveno vajo spodaj. Sam nastanek ZIP-a ne dokazuje uspešne obnove.

Tehnična alternativa, iz projektne mape z aktivnim virtualnim okoljem:

```sh
python manage.py create_backup_snapshot --trigger MANUAL
```

`--trigger` dopušča `MANUAL`, `PRE_MONTHLY`, `POST_MONTHLY`, `SCHEDULED`; privzeto velja `SCHEDULED`. Ukaz preveri SQLite z `PRAGMA quick_check`, ponovno odpre ZIP ter preveri CRC in SHA-256 baze ter odtis objavljenega ZIP-a. Nato shrani zapis uspešne kopije. Odtisa ZIP-a in baze sta v `BackupSnapshot`; odtis baze tudi v `manifest.json`. Izvajalec naj ju pridobi in primerja z dejanskimi datotekami. Zaslon prikazuje le čas in ime.

### Obnovite samo v NOVO mapo

**Obnova ustvari preverjeno obnovitveno mapo. Ne zamenja produkcijske baze, ne spremeni nastavitev poti in ne zažene obnovljene aplikacije. Vmesnika za obnovo ni.**

1. Izberite ustrezen posnetek glede na čas pred težavo. Kopija vsebuje samo podatke do trenutka zajema. Poznejše izdaje in pošiljanja je treba uskladiti ločeno.
2. Z izvajalcem zabeležite trenutne produkcijske poti: bazo, medijske datoteke, lokalni arhiv, iCloud in Dropbox. Če je baza poškodovana, tri arhivske poti pridobite iz predajnega zapisa. Ukaz jih zahteva izrecno in ne bere žive baze.
3. Pripravite obstoječo nadrejeno mapo za obnovitveno vajo, ločeno od produkcijskih lokacij. **Ciljne podmape ne ustvarite vnaprej:** ne sme obstajati, niti kot prazna mapa ali povezava.
4. Preverite prostor za razširjeno kopijo in začasne datoteke. ZIP naj bo zaupanja vreden, v celoti lokalno dostopen.
5. Iz projektne mape izvedite spodnji ukaz. Nadomestite vse oznake v kotnih oklepajih z dejanskimi absolutnimi potmi. Narekovaje ohranite.

```sh
python manage.py restore_backup_snapshot "<POT_DO_POSNETKA.zip>" "<NOVA_OBNOVITVENA_MAPA>" --local-root "<TRENUTNI_LOKALNI_ARHIV>" --icloud-root "<TRENUTNI_ICLOUD_ARHIV>" --dropbox-root "<TRENUTNA_DROPBOX_MAPA>"
```

Prva argumenta sta ZIP in nova ciljna mapa. Vsi trije argumenti `--local-root`, `--icloud-root`, `--dropbox-root` so obvezni: označujejo **zaščitene trenutne lokacije**, ne novih ciljev. Zaščitena sta tudi nastavljena baza in `MEDIA_ROOT`. Če uporabljate `MARJAN_MEDIA_ROOT`, mora kazati na trenutno produkcijsko lokacijo. Ne navajajte izmišljenih poti za obhod zaščite.

Cilj ne sme biti znotraj zaščitenih lokacij ali njihov nadrejeni imenik. Ukaz zaščiti tudi zgodovinske arhivske poti iz posnetka, kadar jih sedanji operacijski sistem prepozna. Pravilni trenutni argumenti so zato nujni.

6. Uspeh zahteva izhod brez napake in sporočilo `Backup restored and verified in a new recovery directory.` s ciljno potjo. Rezultat vsebuje `database/db.sqlite3`, `archive/` in `media/`; `manifest.json` ostane v ZIP-u.
7. Ohranite ZIP, izhod ukaza in obnovitveno mapo za pregled. Ne zaganjajte produkcijskih postopkov iz te mape.

### Kaj je preverjeno in kaj morate pregledati dodatno

Ukaz zavrne nevarne ZIP poti, povezave in podvojene vnose. Preveri CRC, odtis baze, osnovno prepoznavnost sheme, `PRAGMA quick_check` in `PRAGMA foreign_key_check`. Obnovljene baze ne spreminja.

- Za arhivske zapise `VERIFIED` preveri prisotnost lokalne datoteke in njen SHA-256. To ne potrdi trenutnega stanja iCloud kopije ali vseh še nedokončanih arhivskih zapisov.
- Za referencirane logotipe in podpise preveri prisotnost. Če je odtis shranjen, preveri tudi odtis.
- Tabela računov se preveri na zahtevane stolpce. **Ukaz ne opravi vsebinske revizije računov ali zaporedij.** Izvajalec naj obnovljeno bazo pregleda samo za branje: število računov po podjetju, zadnje številke, stanja dostave in odprti mesečni cikel. Primerjajte z evidenco do datuma posnetka.
- Odprite reprezentativne PDF-je iz obnovljenega arhiva. Preverite izdajatelja, številko, zneske in berljivost.
- PRD §11.3 posebej zahteva vse referencirane priloge pogodb. V tej podlagi ni izveden model nalaganja pogodbenih prilog niti samodejna kontrola njihovih referenc. Celotna mapa `media/` se kopira, vendar to **ni dokaz popolnosti prilog**. S seznamom pogodbenih prilog preverite prisotnost in odpiranje vsake datoteke. Če seznama ali priloge ni, zabeležite vrzel in ustavite odobritev obnove.

### Ustavitev, preklop in možnost vrnitve

Ustavite postopek ob napaki ukaza, neujemajočem odtisu, manjkajoči datoteki, nepričakovanem računu ali nejasni izbiri posnetka. Ne popravljajte ZIP-a ali baze, da bi preverjanje prestalo. Z izvajalcem preverite drug ohranjen posnetek. Za nov poskus izberite novo ciljno mapo; obstoječe ne prepisujte.

**Preklop preverjene kopije v produkcijo zahteva nadzorovano preverjanje.** `MARJAN_DB_PATH` izrecno izbere obstoječo bazo SQLite na določeni absolutni poti. Izbira baze sama po sebi ne aktivira obnovljene namestitve in ne zagotovi njene varne uskladitve.

Obnovljena baza lahko vsebuje stare absolutne poti. Pred produkcijskim preklopom ločeno preverite poti obnovljenega arhiva, reference medijskih datotek, številčenje računov in stanja dostav.

`app.provision_database` je namenjen izključno povsem novim namestitvam; nikoli ga ne uporabite za obnovo ali nadgradnjo. Avtomatiziran postopek za produkcijsko aktivacijo obnovljene namestitve še ni izveden.

Izvajalec mora pred preklopom ustaviti vse procese MarjanApp, ohraniti celotno prejšnje stanje, določiti nove poti in preveriti reference. Baze, arhiva ali medijskih datotek **nikoli ne prepisujte ročno med delovanjem aplikacije**. Ne zaženite dveh produkcijskih kopij. Pred odobritvijo uskladite vse račune in pošiljanja po datumu posnetka, da se številke ne uporabijo ponovno in računi ne pošljejo dvakrat.

Načrt vrnitve naj ohrani usklajen komplet baze, arhiva, medijskih datotek in konfiguracije. Vrnitev po novih izdajah zahteva najprej uskladitev novih dokumentov.

### Predajna obnovitvena vaja — zapis dokazov

Izpolnite tudi ob neuspehu. Priložite izhod ukaza in seznam pregledanih datotek; brez gesel.

| Preverjanje | Dokaz / rezultat |
|---|---|
| Datum, izvajalec | ____________________ |
| Platforma, različica OS, Python, različica aplikacije | ____________________ |
| Izbrani ZIP, datum posnetka, nova ciljna mapa | ____________________ |
| SHA-256 ZIP-a in baze; vir pričakovanih odtisov; primerjava | ____________________ |
| Integriteta baze: uspeh ukaza, quick_check, foreign_key_check | ____________________ |
| Računi: podjetja, število, zadnje številke, odprta stanja | ____________________ |
| Referencirane datoteke: arhiv, logotipi, podpisi, seznam vseh prilog | ____________________ |
| Zaščita živih podatkov: zaščitene poti, dokaz nespremenjenosti | ____________________ |
| Končna odobritev uporabnika in izvajalca; omejitve | ____________________ |

Odobritev te vaje potrjuje obnovitveno kopijo. Produkcijski preklop ima ločeno odobritev in zapis preverjanja. Dokler vaja ni izvedena na ciljnem macOS po tem postopku, predaje ne označite kot zaključene.

## 2. Neuspelo pošiljanje e-pošte

**Zaradi napake e-pošte nikoli ne izdajte drugega računa.** Ponovno pošiljanje uporablja isti izdani račun in preverjeni PDF.

| Rezultat | Pomen in varen ukrep |
|---|---|
| `ACCEPTED` | SMTP strežnik je sprejel sporočilo. To ni dokaz, da ga je prejemnik prebral ali da ni poznejše zavrnitve. Ne ponavljajte pošiljanja zaradi same odsotnosti odgovora. |
| `CONFIRMED_FAILURE` | Potrjen neuspeh poskusa. Odpravite vzrok in uporabite dovoljen ponovni poskus. |
| `UNKNOWN` | Po začetku oddaje ni zanesljivega rezultata. Najprej ročno preverite; ne ponavljajte na slepo. |

1. V mesečnem obračunu, korak 6, preglejte **Dostava** po posameznih računih. Zapišite številko, čas poskusa in sporočilo napake.
2. Ob potrjenem neuspehu preverite povezavo, nastavitve pošiljatelja in Keychain. Na podjetju uporabite **Preveri SMTP povezavo**. Test preveri povezavo in prijavo, ne končne dostave računa.
3. Za neuspešne račune uporabite **Pripravi pošiljanje**, nato **Pošlji pripravljene račune**, ko sta dejanji ponujeni. Ne spreminjajte izdanega računa ali PDF-ja.
4. Ob `UNKNOWN` preverite ponudnikovo evidenco pošiljanja, mapo Poslano in po potrebi prejemnika. Sama odsotnost v mapi Poslano ni zadosten dokaz neuspeha SMTP oddaje.
5. Če pošiljanje potrdite, vnesite razlog preverjanja in uporabite **Potrdi, da je bilo poslano**. Če zanesljivo potrdite neuspeh, uporabite **Potrdi, da ni bilo poslano**; aplikacija pripravi varen ponovni poskus. Če rezultat ostaja nejasen, ustavite ponavljanje in vključite izvajalca.

`PREPARED` pomeni pripravljen poskus; `SUBMISSION_STARTED` pomeni začeto oddajo. Ob dolgotrajno nedokončani oddaji ne spreminjajte statusa v bazi. Zahtevajte preverjanje izvajalca.

**Kontrolni dokument za računovodjo:** preverite opravilo na nadzorni plošči, pravilno podjetje in obdobje. Po potrjenem neuspehu uporabite ponujeno **Ponovi pošiljanje** oziroma **Nadaljuj**, če se je vsebina medtem spremenila. `UNKNOWN` ali nedokončana oddaja blokirata ponavljanje; za ta dokument ni izveden enak obrazec razrešitve kot za račune. Potrebno je ročno preverjanje in nadzorovana obravnava. Ne označite ga kot poslanega z ročnim posegom v bazo. Opravilo ne blokira izdaje računov, vendar ostaja odprto.

Zbirno poročilo uporabniku je ločeno od kontrolnega dokumenta računovodji. Tudi zanj ponovitev zahteva `CONFIRMED_FAILURE`; pri `UNKNOWN` preverite ročno in vključite izvajalca.

## 3. Težave s številčenjem računov

Po PRD §6.5 »Številčna zaporedja« ima vsako podjetje dve neodvisni zaporedji:

- `NP`, npr. `NP-00147`: neprekinjeno, brez letnice, brez letnega ponastavljanja.
- `ST`, npr. `ST-2027-0001`: vezano na leto datuma izdaje; novo leto ima svoje zaporedje.

Številka se dodeli ob potrditvi. SQLite uporablja transakcije `IMMEDIATE`; dodelitev številke in izdaja sta atomski. Izdani podatki ostanejo nespremenljivi; dostavni status se lahko spremeni. Storno in dobropis porabita številko istega zaporedja kot izvirnik. Številke ne vračajte v uporabo.

Če številka deluje napačno:

1. Ustavite nadaljnje potrjevanje prizadetega podjetja. Ohranite stanje in izdelajte posnetek, če je kopiranje varno in mogoče.
2. Preverite izdajatelja, tip dokumenta in **datum izdaje**, ne samo obračunskega obdobja. Enaka številka pri različnih podjetjih sama po sebi ni podvojitev.
3. Primerjajte zadnje dejansko izdane dokumente, tudi stornirane dokumente in dobropise. Osnutek ali neizdani kandidat ni porabljena uradna številka.
4. Preverite, ali je nedavno potekala obnova ali selitev. Primerjajte podatke z dokumenti, izdanimi po datumu posnetka.
5. Izvajalcu predajte podjetje, zaporedje, leto, zadnjo pravilno in sporno številko, čas ter izhod napake. Zahtevajte pregled samo za branje in pisni načrt odprave.

**Nikoli ročno ne spreminjajte `NumberSequence`, številk ali vsebine izdanih računov.** Ne zapolnjujte domnevne vrzeli z novim računom. Vmesnik oziroma ukaz za samodejno popravilo zaporedij ni izveden. Ne improvizirajte SQL popravkov.

## 4. Prehod v novo leto

Prehod ne zahteva ročnega ponastavljanja baze. `ST` izbira zaporedje po letu datuma izdaje; `NP` nadaljuje obstoječe. Preverite vsako od petih podjetij posebej.

Pred prvo produkcijsko izdajo novega leta:

- [ ] Izdelajte in preverite posnetek; zabeležite zadnje številke preteklega leta po podjetjih.
- [ ] Preverite datum izdaje in obračunsko obdobje. Januarsko obdobje samo ne določa leta `ST`.
- [ ] Z izvajalcem preverite nadaljevanje `NP` in pravilno letno zaporedje `ST`. Ne izdajte navideznega računa samo za preizkus številčenja.
- [ ] Preglejte aktivne pogodbe, odpovedi, izteke, podaljšanja, nove pogodbe in delna obdobja.
- [ ] Preverite veljavnost podpisnikov, bančne podatke, prejemnike ter potrditvi DDV in vzorca za vsa podjetja.
- [ ] Opravite pregled indeksacijskih pogojev in izjem. Preglejte predhodno kontrolo in paket pred potrditvijo.

Po PRD §6.8 »Indeksacija najemnine« se januarja preveri objavljeni indeks za preteklo leto. Uskladitev velja le ob izpolnjenih pogodbenih pogojih, praviloma od 1. februarja. Pogodbo brez klavzule izključite; preverite prag spremembe in omejitev na zvišanje. Zamujen vnos lahko zahteva poračun na naslednjem računu, nikoli spremembe že izdanega računa.

Izvedene so domenske storitve izračuna in potrditve indeksacije ter predlogov poračuna. **Namenski januarski paketni zaslon in samodejni prenos indeksa nista izvedena.** Indeksacijo, izključitve in poračune zato izvedite le z nadzorovanim preverjanjem izvajalca glede na PRD; ne nadomeščajte jih z neposrednim popravljanjem mesečnih zneskov v bazi.

## 5. Selitev na nov Mac

1. Na starem računalniku ustavite nove izdaje in pošiljanja. Zabeležite zadnje račune in nerešene dostave. Izdelajte preverjen ZIP in ohranite staro namestitev za primerjavo.
2. Prenesite kodo dogovorjene različice, `requirements.txt`, ZIP ter zapis neskrivnih nastavitev in poti. Virtualnega okolja iz Windows ne prenašajte; na Macu ustvarite novo.
3. Na novem Macu pripravite Python, lokalno okolje in odvisnosti po §6. Baza uporablja SQLite, brez ločenega strežnika. Delovna baza ostane lokalna.
4. Prenesite oziroma omogočite lokalno dostopnost arhiva in medijskih datotek. Zabeležite nove ločene poti lokalnega arhiva, iCloud arhiva in Dropbox posnetkov. `MEDIA_ROOT` privzeto kaže na `media` v projektu; nastavitev `MARJAN_MEDIA_ROOT` lahko določi drugo lokacijo.
5. Izvedite obnovitveno vajo iz §1 v novo mapo. Stare poti v obnovljeni bazi se ne prepišejo samodejno. Produkcijsko priključitev baze, arhiva, medijskih datotek in njihovih referenc prepustite nadzorovanemu preverjanju.
6. Ponovno nastavite SMTP poverilnice v **macOS Keychain**. Aplikacija uporablja `keyring`: storitev določa `MARJAN_KEYRING_SERVICE`, ključ uporabnika pa polje podjetja `keychain_kljuc_posiljatelja`. Ločene nastavitve zbirnega poročila uporabljajo svoj ključ. Baza hrani sklice, ne gesel.
7. Preverite vseh pet SMTP povezav in povezavo zbirnega poročila. Nato preverite tri shranjevalne lokacije, vzorce in potrditve. Primerjajte zadnje številke ter odprta stanja s staro namestitvijo.
8. Produkcijo omogočite šele po zapisani odobritvi. Stara kopija ne sme hkrati izdajati ali pošiljati.

**Gesel in Keychain skrivnosti ne shranjujte v ZIP, projekt ali medijske mape.** Kopija ne izvaža Keychaina niti namestitvenega okolja. Ker kopira celoten arhiv in `MEDIA_ROOT`, vanju tudi ročno ne odlagajte datotek z gesli. Ni izvedenega zaslona ali management ukaza za vpis gesel; varni vpis in preverjanje ustreznega Keychain zapisa opravi izvajalec brez beleženja gesla.

## 6. Sveža namestitev in začetna nastavitev

### Tehnična priprava

SAD v1.2 §1 »Odločitev o skladu« določa Python 3.12+, Django 5.2, SQLite in `venv` neposredno na macOS. Potrebujete kodo dogovorjene različice, zapisljive lokalne mape, dostopna iCloud in Dropbox ter SMTP podatke. `requirements.txt` vključuje WeasyPrint, Pillow, lxml in keyring. Sistemskih knjižnic za WeasyPrint repozitorij ne namesti sam; njihovo namestitev in delujoč PDF na konkretnem Macu preveri izvajalec. Popoln avtomatiziran namestitveni postopek za macOS ni priložen.

Za **novo, prazno namestitev**, v terminalu macOS:

```sh
cd "<MAPA_NOVE_NAMESTITVE>"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Nato sledite [varnemu postopku prve namestitve](persistent-paths.md#first-installation-a-brand-new-database-only): izrecno določite `MARJAN_DB_PATH`, `MARJAN_MEDIA_ROOT` in `MARJAN_RENDERING_ROOT` zunaj repozitorija, preverite izpis ukaza `python -m app.provision_database --check-paths` in šele nato izvedite `python -m app.provision_database --confirm-new-install`. Nadrejena mapa baze mora že obstajati; ciljna baza in njene stranske datoteke ne smejo obstajati. Ukaz obstoječe baze nikoli ne prepiše, izbriše, ponastavi ali migrira. Uspeh zahteva izhodno kodo nič in sporočilo `New installation database provisioned and verified:` s preverjeno potjo.

Po uspehu obdržite iste okoljske spremenljivke, izvedite `python manage.py check --database default` in `python manage.py showmigrations --plan` (vse migracije morajo biti označene `[X]`), nato `python manage.py runserver 127.0.0.1:8000`. Ob napaki ne zaganjajte aplikacije; ohranite morebitno ciljno datoteko za diagnostiko in uporabite drugo, še neuporabljeno pot. Običajen zagon ne pripravi manjkajoče izrecno nastavljene baze.

`python3` mora biti nameščeni Python ustrezne različice. Brskalnik odprite na `http://127.0.0.1:8000/`. Strežnik ustavite s `Ctrl+C`. Zagon je lokalni razvojni strežnik, podprt v repozitoriju; ni priloženega produkcijskega zaganjalnika ali samodejnega zagona ob prijavi. Ne uporabite omrežnega naslova `0.0.0.0`.

**Ta korak ni postopek obnove.** Na izvirnem ZIP-u ali preverjeni obnovitveni bazi ne izvajajte migracij. Ob selitvi obstoječih podatkov združljivost različice in morebitne migracije preveri izvajalec pred produkcijskim preklopom.

Pred testom SMTP izvajalec nastavi neskrivne okoljske spremenljivke `MARJAN_SMTP_HOST`, `MARJAN_SMTP_PORT`, `MARJAN_SMTP_SECURITY`, `MARJAN_SMTP_TIMEOUT_SECONDS`, `MARJAN_KEYRING_SERVICE`. Varnost ima izvedene vrednosti `STARTTLS`, `SSL`, `NONE`; uporabite potrjene podatke ponudnika. Geslo vpišite v Keychain, ne med spremenljivke projekta. Repozitorij ne vsebuje samodejnega nalaganja datoteke `.env`.

### Podatki in preverjanje pripravljenosti

PRD v1.2 §16.4 »Prvi zagon in začetna nastavitev« določa celotno začetno verigo. Nova namestitev odpre čarovnik; priprava baze ga ne zaključi in ne dovoli izdaje računov. Napredek se shrani, zato lahko brskalnik zaprete in pozneje nadaljujete. Nastavitev opravite po naslednjem vrstnem redu:

1. **Nastavitve:** vnesite vseh pet podjetij. Preverite naziv, davčne podatke, naslov, IBAN, pošiljatelja, računovodjo in logotip. Dodajte veljavnega podpisnika s podpisom.
2. **Nepremičnine:** dodajte nepremičnine in njihove enote; nato vnesite **Najemnike**.
3. **Pogodbe:** povežite podjetje prek enote z najemnikom. Vnesite veljavnost, rok plačila, prejemnike, postavke, zneske in DDV režime. Preverite reference za posebne DDV režime ter indeksacijske pogoje.
4. **Nastavitve → Arhiv:** vnesite absolutne poti treh že obstoječih map. Lokalni arhiv, iCloud in Dropbox morajo biti ločeni in se ne smejo prekrivati niti z `MEDIA_ROOT`. Kliknite **Preveri in shrani**. Postopek v vsaki mapi zapiše, prebere in odstrani začasno datoteko. Vsak neuspeh rešite pred nadaljevanjem.
5. Za vsako podjetje odprite **Potrditve pred izdajo**. Preglejte razporeditev DDV in jo izrecno potrdite. Izberite ustrezno aktivno pogodbo, odprite vzorčni PDF in preverite logotip, podpis, podjetje, najemnika, postavke, DDV, plačilni rok in IBAN. Uporabite **Potrdi vzorec**.
6. Vzorec mora imeti oznako **VZOREC – NI RAČUN**. Ne porabi uradne številke, se ne pošlje in ne arhivira kot račun. Potrditvi DDV in vzorca sta ločeni; opravi ju uporabnik. Spremembe pomembnih podatkov lahko potrditve zastarajo; ponovno preglejte in potrdite.
7. Začetno stanje zaporedij, zlasti ob prehodu iz prejšnjih evidenc, preverite z izvajalcem. Ni zaslona za ročno nastavljanje zaporedij; ne vnašajte številk neposredno v bazo.

**Pred prvo produkcijsko izdajo zahtevajte:**

- [ ] Vseh pet podjetij je pripravljenih; vsako ima veljavni ločeni potrditvi DDV in vzorca. Samo odsotnost opozorila ni dokaz popolne začetne nastavitve.
- [ ] Vse tri lokacije so prestale zapis, branje in odstranitev preizkusne datoteke; medijske datoteke so dostopne.
- [ ] SMTP povezave in Keychain so preverjeni; naslovi prejemnikov in zbirnega poročila so pravilni.
- [ ] PDF na ciljnem Macu je berljiv; predhodna kontrola ne kaže nerešenih blokad. Prva prava izdaja ni namestitveni test.
- [ ] Številčenje in morebitni prenos starih evidenc sta usklajena; ni druge aktivne produkcijske kopije.
- [ ] ZIP je preverjen; obnovitvena vaja in zaščita živih podatkov sta dokumentirani. Nadzorovani produkcijski preklop je ločeno odobren, če je potreben.

V v1 velja `FEATURE_ESLOG=False`. Ne pričakujte izdelave ali pošiljanja eSLOG XML v trenutnem uporabniškem postopku. Pripravljenost produkcije, obnova na macOS in manjkajoči začetni oziroma januarski postopki zahtevajo predajno preverjanje; ta dokument jih ne razglaša za zaključene.

**Izvedbena sled:** `archive/management/commands/`, `archive/backup.py`, `archive/restore.py`, `archive/configuration.py`, `invoicing/backup_hooks.py`, `delivery/services.py`, `delivery/transport.py`, `accounting_export/workflow.py`, `companies/services.py`, `invoicing/services.py`, `contracts/services.py`, `ui/views.py`, `ui/urls.py`, `ui/templates/ui/`, `app/settings.py`, `requirements.txt`, `manage.py`. Testi v `tests/archive/test_backup.py` in `tests/archive/test_restore.py` ne nadomestijo predajne vaje na Macu.
