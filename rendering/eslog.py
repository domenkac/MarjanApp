from collections import defaultdict
from pathlib import Path

from lxml import etree

from core.eslog_konfiguracija import (
    ELEKTRONSKI_NASLOV_KUPCA,
    ELEKTRONSKI_NASLOV_KUPCA_VKLJUCEN,
    KONTAKTNA_TOCKA_KUPCA,
    KONTAKTNI_EMAIL_KUPCA,
    KONTAKTNI_PODATKI_KUPCA_VKLJUCENI,
    KONTAKTNI_TELEFON_KUPCA,
    EslogKonfiguracijaNapaka,
    validiraj_eslog_konfiguracija_snapshot,
)
from invoicing.models import Invoice, InvoiceLine

from .contexts import (
    EslogContext,
    EslogKonfiguracijaContext,
    EslogRenderNapaka,
    EslogVrstica,
)

NS = "urn:eslog:2.00"
_XSD_DIR = Path(__file__).parent / "xsd" / "eslog_2_0"
_XSD_PATH = _XSD_DIR / "eSLOG20_INVOIC_v200.xsd"
_DDV_KODE = {"S", "E", "AE", "O"}


class _LokalniXsdResolver(etree.Resolver):
    def resolve(self, url, public_id, context):
        glavna_shema = _XSD_PATH.resolve()
        podpisna_shema = (_XSD_DIR / "xmldsig-core-schema.xsd").resolve()
        dovoljeni = {
            str(glavna_shema): glavna_shema,
            f"file:/{glavna_shema.as_posix()}": glavna_shema,
            "xmldsig-core-schema.xsd": podpisna_shema,
            f"file:/{podpisna_shema.as_posix()}": podpisna_shema,
        }
        pot = dovoljeni.get(url)
        if pot is None:
            raise OSError(f"XSD vir ni dovoljen: {url}")
        return self.resolve_filename(str(pot), context)


def _varen_parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
    )


def nalozi_eslog_shemo() -> etree.XMLSchema:
    parser = _varen_parser()
    parser.resolvers.add(_LokalniXsdResolver())
    try:
        return etree.XMLSchema(etree.parse(str(_XSD_PATH), parser))
    except (OSError, etree.XMLSyntaxError, etree.XMLSchemaParseError) as napaka:
        raise EslogRenderNapaka(
            "ESLOG_SHEMA_NEVELJAVNA", "Lokalne eSLOG sheme ni bilo mogoče naložiti."
        ) from napaka


def validiraj_eslog(xml_bytes: bytes) -> None:
    try:
        dokument = etree.fromstring(xml_bytes, _varen_parser())
        nalozi_eslog_shemo().assertValid(dokument)
    except (
        etree.XMLSyntaxError,
        etree.DocumentInvalid,
        etree.XMLSchemaValidateError,
    ) as napaka:
        raise EslogRenderNapaka(
            "ESLOG_XSD_NEVELJAVEN", f"eSLOG XML ni veljaven: {napaka}"
        ) from napaka


def _q(ime: str) -> str:
    return f"{{{NS}}}{ime}"


def _dodaj(stars, ime: str, vrednost=None):
    element = etree.SubElement(stars, _q(ime))
    if vrednost is not None:
        element.text = str(vrednost)
    return element


def _znesek(vrednost) -> str:
    return f"{vrednost:.2f}"


def _datum(vrednost) -> str:
    return vrednost.isoformat()


def _dtm(stars, kvalifikator: str, datum) -> None:
    dtm = _dodaj(stars, "S_DTM")
    c507 = _dodaj(dtm, "C_C507")
    _dodaj(c507, "D_2005", kvalifikator)
    if datum is not None:
        _dodaj(c507, "D_2380", _datum(datum))


def _ftx(stars, kvalifikator: str, besedilo: str) -> None:
    ftx = _dodaj(stars, "S_FTX")
    _dodaj(ftx, "D_4451", kvalifikator)
    c108 = _dodaj(ftx, "C_C108")
    _dodaj(c108, "D_4440", besedilo)


def _rff(stars, kvalifikator: str, vrednost: str, datum=None) -> None:
    sg1 = _dodaj(stars, "G_SG1")
    rff = _dodaj(sg1, "S_RFF")
    c506 = _dodaj(rff, "C_C506")
    _dodaj(c506, "D_1153", kvalifikator)
    _dodaj(c506, "D_1154", vrednost)
    if datum is not None:
        _dtm(sg1, "384", datum)


def _party(
    stars,
    *,
    tip: str,
    naziv: str,
    naslov: str,
    drzava_koda: str,
    davcna: str,
    ddv_identifikacijska: str,
    maticna: str,
    identificiran_za_ddv: bool,
    samo_ahp: bool,
    trr=None,
):
    sg2 = _dodaj(stars, "G_SG2")
    nad = _dodaj(sg2, "S_NAD")
    _dodaj(nad, "D_3035", tip)
    c080 = _dodaj(nad, "C_C080")
    _dodaj(c080, "D_3036", naziv)
    c059 = _dodaj(nad, "C_C059")
    _dodaj(c059, "D_3042", naslov)
    _dodaj(nad, "D_3207", drzava_koda)
    if trr:
        fii = _dodaj(sg2, "S_FII")
        _dodaj(fii, "D_3035", "RB")
        c078 = _dodaj(fii, "C_C078")
        _dodaj(c078, "D_3194", trr.replace(" ", ""))
        _dodaj(c078, "D_3192", naziv)
    reference = []
    if identificiran_za_ddv and not samo_ahp:
        vat_id = f"SI{davcna}" if drzava_koda == "SI" and davcna else ""
        if drzava_koda != "SI":
            vat_id = ddv_identifikacijska
        if not vat_id:
            raise EslogRenderNapaka(
                "DDV_IDENTIFIKATOR_MANJKA",
                "Partner, identificiran za DDV, nima DDV identifikacijske številke.",
            )
        reference.append(("VA", vat_id))
    if davcna:
        reference.append(("AHP", davcna))
    if maticna:
        reference.append(("0199", maticna))
    for kvalifikator, vrednost in reference:
        sg3 = _dodaj(sg2, "G_SG3")
        rff = _dodaj(sg3, "S_RFF")
        c506 = _dodaj(rff, "C_C506")
        _dodaj(c506, "D_1153", kvalifikator)
        _dodaj(c506, "D_1154", vrednost)
    return sg2


def _com(stars, vrednost: str, kvalifikator: str) -> None:
    com = _dodaj(stars, "S_COM")
    c076 = _dodaj(com, "C_C076")
    _dodaj(c076, "D_3148", vrednost)
    _dodaj(c076, "D_3155", kvalifikator)


def _buyer_optional(sg2, konfiguracija: EslogKonfiguracijaContext) -> None:
    if konfiguracija.elektronski_naslov_kupca_vkljucen:
        sg5 = _dodaj(sg2, "G_SG5")
        cta = _dodaj(sg5, "S_CTA")
        _dodaj(cta, "D_3139", "IC")
        _com(sg5, konfiguracija.elektronski_naslov_kupca, "EM")
    if konfiguracija.kontaktni_podatki_kupca_vkljuceni:
        sg5 = _dodaj(sg2, "G_SG5")
        cta = _dodaj(sg5, "S_CTA")
        _dodaj(cta, "D_3139", "PD")
        if konfiguracija.kontaktna_tocka_kupca:
            c056 = _dodaj(cta, "C_C056")
            _dodaj(c056, "D_3412", konfiguracija.kontaktna_tocka_kupca)
        if konfiguracija.kontaktni_telefon_kupca:
            _com(sg5, konfiguracija.kontaktni_telefon_kupca, "TE")
        if konfiguracija.kontaktni_email_kupca:
            _com(sg5, konfiguracija.kontaktni_email_kupca, "EM")


def _tax(stars, koda: str, stopnja) -> None:
    tax = _dodaj(stars, "S_TAX")
    _dodaj(tax, "D_5283", "7")
    c241 = _dodaj(tax, "C_C241")
    _dodaj(c241, "D_5153", "VAT")
    c243 = _dodaj(tax, "C_C243")
    _dodaj(c243, "D_5278", _znesek(stopnja))
    _dodaj(tax, "D_5305", koda)


def _moa(stars, kvalifikator: str, vrednost) -> None:
    moa = _dodaj(stars, "S_MOA")
    c516 = _dodaj(moa, "C_C516")
    _dodaj(c516, "D_5025", kvalifikator)
    _dodaj(c516, "D_5004", _znesek(vrednost))
    _dodaj(c516, "D_6345", "EUR")


def pripravi_eslog_context(invoice: Invoice) -> EslogContext:
    if not isinstance(invoice, Invoice) or invoice.pk is None:
        raise EslogRenderNapaka("RACUN_NE_OBSTAJA", "Račun ne obstaja.")
    svezi = Invoice.objects.filter(pk=invoice.pk).first()
    if svezi is None:
        raise EslogRenderNapaka("RACUN_NE_OBSTAJA", "Račun ne obstaja.")
    if not svezi.stevilka:
        raise EslogRenderNapaka("STEVILKA_MANJKA", "Račun nima številke.")
    try:
        snapshot = validiraj_eslog_konfiguracija_snapshot(
            svezi.eslog_konfiguracija_snapshot
        )
    except EslogKonfiguracijaNapaka as napaka:
        raise EslogRenderNapaka(
            "ESLOG_KONFIGURACIJA_SNAPSHOT_NEVELJAVEN",
            "eSLOG konfiguracijski snapshot ni veljaven.",
        ) from napaka
    segmenti = snapshot["segments"]
    polja = snapshot["fields"]
    obvezna = (
        "prejemnik_naziv",
        "prejemnik_naslov",
        "prejemnik_drzava_koda",
        "prejemnik_tip",
        "izdajatelj_naziv",
        "izdajatelj_naslov",
        "izdajatelj_drzava_koda",
        "izdajatelj_davcna_stevilka",
        "izdajatelj_trr",
    )
    if (
        svezi.prejemnik_identificiran_za_ddv is None
        or svezi.izdajatelj_identificiran_za_ddv is None
    ):
        raise EslogRenderNapaka(
            "SNAPSHOT_MANJKA", "Manjka posnetek identifikacije za DDV."
        )
    for polje in obvezna:
        if not str(getattr(svezi, polje, "") or "").strip():
            raise EslogRenderNapaka(
                "SNAPSHOT_MANJKA", f"Manjka obvezen podatek: {polje}."
            )
    if svezi.prejemnik_tip == "FIZICNA_OSEBA" and not svezi.prejemnik_davcna_stevilka:
        raise EslogRenderNapaka(
            "ESLOG_FIZICNA_OSEBA_BREZ_DAVCNE",
            "eSLOG zapis fizične osebe brez davčne številke ni opredeljen.",
        )
    vrstice = tuple(InvoiceLine.objects.filter(invoice_id=svezi.pk).order_by("pk"))
    if not vrstice:
        raise EslogRenderNapaka("VRSTICE_MANJKAJO", "Račun nima vrstic.")
    for vrstica in vrstice:
        if vrstica.ddv_eslog_koda not in _DDV_KODE:
            raise EslogRenderNapaka(
                "DDV_ESLOG_KODA_NEVELJAVNA", "Vrstica nima veljavne eSLOG DDV kode."
            )
    kode = {vrstica.ddv_eslog_koda for vrstica in vrstice}
    if "O" in kode and "S" in kode:
        raise EslogRenderNapaka(
            "DDV_ESLOG_KOMBINACIJA_NEVELJAVNA",
            "eSLOG račun ne sme združevati kategorije O in obdavčenih postavk.",
        )
    for vrstica in vrstice:
        if vrstica.ddv_eslog_koda in {"E", "AE"} and not vrstica.ddv_klavzula.strip():
            raise EslogRenderNapaka(
                "DDV_KLAVZULA_MANJKA",
                "Vrstica kategorije E ali AE nima obvezne DDV klavzule.",
            )
    if (
        sum((v.osnova for v in vrstice), 0) != svezi.osnova_skupaj
        or sum((v.ddv_znesek for v in vrstice), 0) != svezi.ddv_skupaj
        or sum((v.skupaj for v in vrstice), 0) != svezi.skupaj_z_ddv
    ):
        raise EslogRenderNapaka(
            "VSOTE_NESKLADNE", "Vsote računa se ne ujemajo z vrsticami."
        )

    original = None
    if svezi.document_type in (
        Invoice.DocumentType.DOBROPIS,
        Invoice.DocumentType.STORNO,
    ):
        original = Invoice.objects.filter(pk=svezi.povezan_racun_id).first()
        if original is None or not original.stevilka:
            raise EslogRenderNapaka(
                "KOREKCIJA_NEPOPOLNA", "Korekcija nima veljavnega izvirnega računa."
            )
    return EslogContext(
        document_type=svezi.document_type,
        stevilka=svezi.stevilka,
        datum_izdaje=svezi.datum_izdaje,
        rok_placila=svezi.rok_placila,
        prejemnik_naziv=svezi.prejemnik_naziv,
        prejemnik_naslov=svezi.prejemnik_naslov,
        prejemnik_drzava_koda=svezi.prejemnik_drzava_koda,
        prejemnik_identificiran_za_ddv=svezi.prejemnik_identificiran_za_ddv,
        prejemnik_davcna_stevilka=svezi.prejemnik_davcna_stevilka,
        prejemnik_ddv_identifikacijska_stevilka=(
            svezi.prejemnik_ddv_identifikacijska_stevilka
        ),
        prejemnik_maticna_stevilka=svezi.prejemnik_maticna_stevilka,
        prejemnik_tip=svezi.prejemnik_tip,
        izdajatelj_naziv=svezi.izdajatelj_naziv,
        izdajatelj_naslov=svezi.izdajatelj_naslov,
        izdajatelj_drzava_koda=svezi.izdajatelj_drzava_koda,
        izdajatelj_identificiran_za_ddv=svezi.izdajatelj_identificiran_za_ddv,
        izdajatelj_davcna_stevilka=svezi.izdajatelj_davcna_stevilka,
        izdajatelj_maticna_stevilka=svezi.izdajatelj_maticna_stevilka,
        izdajatelj_trr=svezi.izdajatelj_trr,
        oznaka_pogodbe=svezi.oznaka_pogodbe,
        povezan_racun_stevilka=original.stevilka if original else None,
        povezan_racun_datum=original.datum_izdaje if original else None,
        osnova_skupaj=svezi.osnova_skupaj,
        ddv_skupaj=svezi.ddv_skupaj,
        skupaj_z_ddv=svezi.skupaj_z_ddv,
        vrstice=tuple(
            EslogVrstica(
                v.opis,
                v.osnova,
                v.ddv_stopnja,
                v.ddv_znesek,
                v.skupaj,
                v.ddv_eslog_koda,
                v.ddv_klavzula,
            )
            for v in vrstice
        ),
        eslog_konfiguracija=EslogKonfiguracijaContext(
            elektronski_naslov_kupca_vkljucen=segmenti[
                ELEKTRONSKI_NASLOV_KUPCA_VKLJUCEN
            ],
            kontaktni_podatki_kupca_vkljuceni=segmenti[
                KONTAKTNI_PODATKI_KUPCA_VKLJUCENI
            ],
            elektronski_naslov_kupca=polja.get(ELEKTRONSKI_NASLOV_KUPCA),
            kontaktna_tocka_kupca=polja.get(KONTAKTNA_TOCKA_KUPCA),
            kontaktni_telefon_kupca=polja.get(KONTAKTNI_TELEFON_KUPCA),
            kontaktni_email_kupca=polja.get(KONTAKTNI_EMAIL_KUPCA),
        ),
    )


def sestavi_eslog(context: EslogContext) -> bytes:
    root = etree.Element(_q("Invoice"), nsmap={None: NS})
    dokument = _dodaj(root, "M_INVOIC")
    dokument.set("Id", "data")
    unh = _dodaj(dokument, "S_UNH")
    _dodaj(unh, "D_0062", context.stevilka)
    s009 = _dodaj(unh, "C_S009")
    for ime, vrednost in (
        ("D_0065", "INVOIC"),
        ("D_0052", "D"),
        ("D_0054", "01B"),
        ("D_0051", "UN"),
    ):
        _dodaj(s009, ime, vrednost)
    bgm = _dodaj(dokument, "S_BGM")
    c002 = _dodaj(bgm, "C_C002")
    _dodaj(
        c002,
        "D_1001",
        "381"
        if context.document_type
        in (Invoice.DocumentType.DOBROPIS, Invoice.DocumentType.STORNO)
        else "380",
    )
    c106 = _dodaj(bgm, "C_C106")
    _dodaj(c106, "D_1004", context.stevilka)
    _dtm(dokument, "137", context.datum_izdaje)
    klavzule = dict.fromkeys(
        vrstica.ddv_klavzula.strip()
        for vrstica in context.vrstice
        if vrstica.ddv_eslog_koda in {"E", "AE"} and vrstica.ddv_klavzula.strip()
    )
    for klavzula in klavzule:
        _ftx(dokument, "AGM", klavzula)
    if context.oznaka_pogodbe:
        _rff(dokument, "CT", context.oznaka_pogodbe)
    if context.povezan_racun_stevilka:
        _rff(
            dokument,
            "OI",
            context.povezan_racun_stevilka,
            context.povezan_racun_datum,
        )
    samo_ahp = any(vrstica.ddv_eslog_koda == "O" for vrstica in context.vrstice)
    buyer = _party(
        dokument,
        tip="BY",
        naziv=context.prejemnik_naziv,
        naslov=context.prejemnik_naslov,
        drzava_koda=context.prejemnik_drzava_koda,
        davcna=context.prejemnik_davcna_stevilka,
        ddv_identifikacijska=context.prejemnik_ddv_identifikacijska_stevilka,
        maticna=context.prejemnik_maticna_stevilka,
        identificiran_za_ddv=context.prejemnik_identificiran_za_ddv,
        samo_ahp=samo_ahp,
    )
    _buyer_optional(buyer, context.eslog_konfiguracija)
    _party(
        dokument,
        tip="SE",
        naziv=context.izdajatelj_naziv,
        naslov=context.izdajatelj_naslov,
        drzava_koda=context.izdajatelj_drzava_koda,
        davcna=context.izdajatelj_davcna_stevilka,
        ddv_identifikacijska="",
        maticna=context.izdajatelj_maticna_stevilka,
        identificiran_za_ddv=context.izdajatelj_identificiran_za_ddv,
        samo_ahp=samo_ahp,
        trr=context.izdajatelj_trr,
    )
    sg7 = _dodaj(dokument, "G_SG7")
    cux = _dodaj(sg7, "S_CUX")
    c504 = _dodaj(cux, "C_C504")
    _dodaj(c504, "D_6347", "2")
    _dodaj(c504, "D_6345", "EUR")
    if context.rok_placila and context.document_type in (
        Invoice.DocumentType.NP,
        Invoice.DocumentType.ST,
    ):
        sg8 = _dodaj(dokument, "G_SG8")
        pat = _dodaj(sg8, "S_PAT")
        _dodaj(pat, "D_4279", "1")
        _dtm(sg8, "13", context.rok_placila)

    for stevilka, vrstica in enumerate(context.vrstice, 1):
        sg26 = _dodaj(dokument, "G_SG26")
        lin = _dodaj(sg26, "S_LIN")
        _dodaj(lin, "D_1082", stevilka)
        imd = _dodaj(sg26, "S_IMD")
        _dodaj(imd, "D_7077", "F")
        c273 = _dodaj(imd, "C_C273")
        _dodaj(c273, "D_7008", vrstica.opis)
        qty = _dodaj(sg26, "S_QTY")
        c186 = _dodaj(qty, "C_C186")
        _dodaj(c186, "D_6063", "47")
        _dodaj(c186, "D_6060", "1")
        _dodaj(c186, "D_6411", "C62")
        sg27 = _dodaj(sg26, "G_SG27")
        _moa(sg27, "203", vrstica.osnova)
        sg29 = _dodaj(sg26, "G_SG29")
        pri = _dodaj(sg29, "S_PRI")
        c509 = _dodaj(pri, "C_C509")
        _dodaj(c509, "D_5125", "AAA")
        _dodaj(c509, "D_5118", _znesek(vrstica.osnova))
        sg34 = _dodaj(sg26, "G_SG34")
        _tax(sg34, vrstica.ddv_eslog_koda, vrstica.ddv_stopnja)
        _moa(sg34, "125", vrstica.osnova)
        _moa(sg34, "124", vrstica.ddv_znesek)

    uns = _dodaj(dokument, "S_UNS")
    _dodaj(uns, "D_0081", "D")
    for kvalifikator, vrednost in (
        ("79", context.osnova_skupaj),
        ("389", context.osnova_skupaj),
        ("388", context.skupaj_z_ddv),
        ("9", context.skupaj_z_ddv),
        ("176", context.ddv_skupaj),
    ):
        sg50 = _dodaj(dokument, "G_SG50")
        _moa(sg50, kvalifikator, vrednost)

    skupine = defaultdict(lambda: [0, 0])
    for vrstica in context.vrstice:
        kljuc = (vrstica.ddv_eslog_koda, vrstica.ddv_stopnja)
        skupine[kljuc][0] += vrstica.osnova
        skupine[kljuc][1] += vrstica.ddv_znesek
    for (koda, stopnja), (osnova, ddv) in skupine.items():
        sg52 = _dodaj(dokument, "G_SG52")
        _tax(sg52, koda, stopnja)
        _moa(sg52, "125", osnova)
        _moa(sg52, "124", ddv)
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True)


def render_eslog(invoice: Invoice) -> bytes:
    xml_bytes = sestavi_eslog(pripravi_eslog_context(invoice))
    validiraj_eslog(xml_bytes)
    return xml_bytes
