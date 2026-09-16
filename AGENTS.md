# MarjanApp – razvojna pravila

## Vir resnice

Projektna dokumentacija je v `docs/`.

* `01_PRD_v1.2.md` — poslovne zahteve, cilji in obseg.
* `03_SAD_v1.2.md` — arhitektura in tehnične odločitve.
* `04_FSD_Faza1_v1.2_FINAL.md` — podrobna specifikacija faze 1.

PRD, SAD in FSD so glavni vir projektnih zahtev.

Ob neskladju:

* poslovna zahteva → PRD,
* arhitektura → SAD,
* podrobnosti implementacije → FSD.

Če dokumenti dejansko nasprotujejo drug drugemu, ne izberi rešitve sam.

## Delo z dokumentacijo

Pred načrtovanjem ali implementacijo poišči samo razdelke, neposredno povezane s trenutno nalogo.

Ne beri celotnega PRD, SAD ali FSD, če to ni potrebno.

Najprej poišči relevantno poglavje oziroma izraz in preberi najmanjši potreben kontekst.

Druge dokumente odpri samo:

* če se relevantni razdelek nanje izrecno sklicuje,
* če potrebuješ podatek, ki v trenutnem viru manjka,
* ali če preverjaš morebitno neskladje.

Ne povzemaš že znane projektne dokumentacije, razen če uporabnik to zahteva.

Pri pomembnih ugotovitvah navedi dokument in naslov razdelka.

## Manjkajoče odločitve

Če odločitev ni določena v dokumentaciji, ne ugibaj.

Napiši:

»Odločitev ni opredeljena v projektni dokumentaciji.«

Nato:

1. na kratko pojasni, zakaj je odločitev potrebna,
2. po potrebi podaj priporočilo,
3. zastavi samo eno konkretno vprašanje,
4. počakaj na odgovor.

Nova odločitev velja šele po izrecni potrditvi uporabnika.

Če vpliva na poslovna pravila, podatkovni model, varnost, račune ali uporabniški postopek, brez potrditve ne implementiraj.

Po potrditvi predlagaj, v kateri dokument oziroma razdelek naj se odločitev zapiše.

## Arhitektura

* Django aplikacije ločuj po odgovornostih.
* Modeli naj ostanejo tanki.
* Poslovna pravila sodijo v `services.py` oziroma namenske domenske module.
* Poslovna logika ne sme biti v views, templates ali admin akcijah.
* Domenska logika mora biti testabilna brez uporabniškega vmesnika.
* Denarne vrednosti vedno uporabljajo `Decimal`, nikoli `float`.
* Spremembe podatkovne strukture izvajaj z Django migracijami.
* Django posluša samo na `127.0.0.1`.

Podrobna poslovna pravila za račune, številčenje, arhiv, pošiljanje in uporabniške postopke vedno preveri v PRD/FSD; ne zanašaj se na povzetke v tem dokumentu.

## Varnost

* Skrivnosti in žetonov ne shranjuj v kodi, Git repozitoriju ali dnevnikih.
* Poverilnice uporabljajo macOS Keychain prek `keyring`, kjer to določa dokumentacija.
* Ohrani Django CSRF zaščito in escaping.
* Vse zunanje vnose preveri na strežniški strani.
* Ne omogoči dostopa do datotek zunaj dovoljenih poti.

## Testiranje

Pri spremembi poslovnega pravila najprej preveri obstoječe teste in po potrebi dodaj oziroma spremeni test.

Posebej skrbno testiraj denarne izračune, DDV, zaokroževanje, številčenje in druga pravila, ki jih FSD označuje kot kritična.

Pred zaključkom naloge:

* zaženi relevantne teste,
* zaženi `ruff check` za spremenjeno kodo,
* preveri rezultat.

Ne izvajaj celotnega testnega paketa, kadar je mogoče spremembo zanesljivo preveriti z ožjim relevantnim naborom testov.

## Razvojni postopek

* Superpowers določa razvojni postopek.
* ECC skills uporabljaj, kadar so relevantni za trenutno nalogo.
* Vibe Engineering pravila uporabljaj samo, kadar so relevantna.

Potrjene zahteve PRD/SAD/FSD imajo prednost pred pomožnimi razvojnimi navodili.

## Omejitev obsega

Implementiraj samo zahtevano nalogo.

Ne dodajaj funkcij, refaktorjev ali posplošitev, ki niso potrebni za trenutno nalogo.

Če odkriješ drugo težavo, jo omeni ločeno; ne rešuj je samodejno.

Na koncu odgovora bodi kratek:

* kaj je bilo spremenjeno,
* kateri testi so bili izvedeni,
* ali ostaja kakšno odprto vprašanje.

## Varčevanje s kontekstom

- Ne beri celotnega PRD, SAD ali FSD, razen če je to nujno.
- Najprej z `rg` poišči relevantni izraz ali naslov razdelka.
- Nato preberi samo najmanjši potreben okoliški kontekst.
- Druge dokumente odpri samo, če se relevantni razdelek nanje sklicuje,
  če manjka potreben podatek ali če preverjaš neskladje.
- Ne ponavljaj vsebine dokumentacije v odgovoru, če to ni potrebno.
- Ne izvajaj širokega pregleda celotnega repozitorija brez razloga.
