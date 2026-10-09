import calendar
from datetime import date

from django import forms
from django.utils import timezone

from accounting_export.arbitrary_export import validate_arbitrary_date_range
from archive.configuration import ArchiveConfigurationError, validate_archive_locations
from catalogs.models import ESLOG_KODE_DDV, ServiceCatalogItem, VatRegime
from companies.models import Company, Signatory
from contracts.models import Lease, LeaseItem, Property, RecipientContact, Tenant, Unit
from delivery.models import MonthlySummaryConfiguration


class AccountantExportPeriodForm(forms.Form):
    od = forms.DateField(
        label="Od", input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        error_messages={"invalid": "Vnesite veljaven datum."},
    )
    do = forms.DateField(
        label="Do", input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        error_messages={"invalid": "Vnesite veljaven datum."},
    )

    def clean(self):
        cleaned = super().clean()
        if "od" in cleaned and "do" in cleaned:
            validate_arbitrary_date_range(cleaned["od"], cleaned["do"])
        return cleaned


class AccountantCompanyMultipleChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return obj.naziv


class AccountantArbitraryExportForm(AccountantExportPeriodForm):
    podjetja = AccountantCompanyMultipleChoiceField(
        label="Podjetja", queryset=Company.objects.order_by("-aktivno", "naziv", "pk"),
        error_messages={
            "required": "Izberite vsaj eno podjetje.",
            "invalid_choice": "Izberite veljavna podjetja.",
            "invalid_pk_value": "Izberite veljavna podjetja.",
        },
    )


class AccountantArbitraryDownloadForm(AccountantExportPeriodForm):
    company_id = forms.ModelChoiceField(
        label="Podjetje", queryset=Company.objects.all(),
        error_messages={"invalid_choice": "Izberite veljavno podjetje."},
    )


class MonthlySummaryConfigurationForm(forms.ModelForm):
    class Meta:
        model = MonthlySummaryConfiguration
        fields = ("email", "keychain_credential_key")
        labels = {  # noqa: RUF012
            "email": "E-naslov za zbirno poročilo",
            "keychain_credential_key": "Ključ poverilnice v Keychainu",
        }
        error_messages = {  # noqa: RUF012
            "email": {"invalid": "Vnesite veljaven e-poštni naslov."},
        }


class ArchiveConfigurationForm(forms.Form):
    local_root = forms.CharField(
        label="Lokalni arhiv",
        help_text="Vnesite absolutno pot do obstoječe mape.",
    )
    icloud_root = forms.CharField(
        label="iCloud arhiv",
        help_text="Vnesite absolutno pot do obstoječe mape.",
    )

    dropbox_root = forms.CharField(
        label="Dropbox varnostne kopije",
        help_text="Vnesite absolutno pot do obstoječe mape.",
    )

    def clean(self):
        cleaned = super().clean()
        if not all(name in cleaned for name in ("local_root", "icloud_root", "dropbox_root")):
            return cleaned
        try:
            roots = validate_archive_locations(cleaned["local_root"], cleaned["icloud_root"], cleaned["dropbox_root"])
        except ArchiveConfigurationError as error:
            raise forms.ValidationError(str(error)) from None
        cleaned["local_root"] = str(roots.local_root)
        cleaned["icloud_root"] = str(roots.icloud_root)
        cleaned["dropbox_root"] = str(roots.dropbox_root)
        return cleaned


class CompanyChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.naziv


class SampleLeaseChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return (
            f"{obj.unit.property.naziv} — {obj.unit.oznaka} — "
            f"{obj.oznaka_pogodbe} — {obj.tenant.naziv}"
        )


class SampleConfirmationForm(forms.Form):
    lease = SampleLeaseChoiceField(
        queryset=Lease.objects.none(), label="Pogodba za vzorec"
    )

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lease"].queryset = Lease.objects.filter(
            unit__property__company=company,
            arhivirana=False,
            unit__aktivna=True,
            unit__property__aktivna=True,
            unit__property__company__aktivno=True,
            tenant__aktiven=True,
            leaseitem__arhivirana=False,
        ).select_related("unit__property", "tenant").order_by(
            "unit__property__naziv", "unit__oznaka", "oznaka_pogodbe", "pk"
        ).distinct()


class SampleLeaseSelectionForm(forms.Form):
    vzorec_pogodba = SampleLeaseChoiceField(
        queryset=Lease.objects.none(), label="Pogodba za vzorec"
    )

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vzorec_pogodba"].queryset = SampleConfirmationForm(
            company=company
        ).fields["lease"].queryset


class PropertyForm(forms.ModelForm):
    company = CompanyChoiceField(queryset=Company.objects.none(), label="Podjetje")

    class Meta:
        model = Property
        fields = ("company", "naziv", "naslov")
        labels = {"company": "Podjetje", "naziv": "Naziv", "naslov": "Naslov"}  # noqa: RUF012

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        company_ids = Company.objects.filter(aktivno=True).values("pk")
        if self.instance.pk:
            company_ids = company_ids.union(
                Company.objects.filter(pk=self.instance.company_id).values("pk")
            )
        self.fields["company"].queryset = Company.objects.filter(
            pk__in=company_ids
        ).order_by("naziv", "pk")


class UnitForm(forms.ModelForm):
    class Meta:
        model = Unit
        fields = ("oznaka", "tip_enote", "povrsina_m2")
        labels = {  # noqa: RUF012
            "oznaka": "Oznaka",
            "tip_enote": "Tip enote",
            "povrsina_m2": "Površina (m²)",
        }


class TenantForm(forms.ModelForm):
    class Meta:
        model = Tenant
        fields = (
            "tip", "naziv", "davcna_stevilka",
            "ddv_identifikacijska_stevilka", "maticna_stevilka", "naslov",
            "drzava_koda", "identificiran_za_ddv",
            "ddv_zavezanec_s_polnim_odbitkom", "email",
        )
        labels = {  # noqa: RUF012
            "tip": "Tip", "naziv": "Naziv",
            "davcna_stevilka": "Davčna številka",
            "ddv_identifikacijska_stevilka": "DDV identifikacijska številka",
            "maticna_stevilka": "Matična številka", "naslov": "Naslov",
            "drzava_koda": "Koda države",
            "identificiran_za_ddv": "Identificiran za DDV",
            "ddv_zavezanec_s_polnim_odbitkom": "DDV zavezanec s polnim odbitkom",
            "email": "E-pošta",
        }
        help_texts = {  # noqa: RUF012
            "drzava_koda": "Dvočrkovna koda države, na primer SI.",
            "ddv_identifikacijska_stevilka": (
                "Za tujega prejemnika, identificiranega za DDV, vnesite polni VAT ID."
            ),
        }
        error_messages = {  # noqa: RUF012
            "email": {"invalid": "Vnesite veljaven e-poštni naslov."},
        }


class UnitChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.property.company.naziv} — {obj.property.naziv} — {obj.oznaka}"


class TenantChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.naziv


class VatRegimeChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.sifra} — {obj.naziv}"


class LeaseForm(forms.ModelForm):
    unit = UnitChoiceField(queryset=Unit.objects.none(), label="Enota")
    tenant = TenantChoiceField(queryset=Tenant.objects.none(), label="Najemnik")
    rok_placila_dni = forms.IntegerField(
        required=False,
        label="Rok plačila (dni)",
        help_text="Pri novi pogodbi prazno polje uporabi privzeti rok podjetja.",
    )

    class Meta:
        model = Lease
        fields = (
            "unit", "tenant", "oznaka_pogodbe", "tip_trajanja",
            "datum_zacetka", "datum_konca", "samodejno_podaljsanje",
            "obdobje_podaljsanja_mesecev", "odpovedni_rok_mesecev",
            "datum_odpovedi", "datum_prenehanja", "status",
            "rok_placila_dni", "ima_klavzulo_indeksacije",
            "opis_pogoja_indeksacije", "tip_pogoja_indeksacije",
            "prag_spremembe_odstotek",
        )
        labels = {  # noqa: RUF012
            "oznaka_pogodbe": "Številka pogodbe",
            "tip_trajanja": "Tip trajanja",
            "datum_zacetka": "Datum začetka",
            "datum_konca": "Datum konca",
            "samodejno_podaljsanje": "Samodejno podaljšanje",
            "obdobje_podaljsanja_mesecev": "Obdobje podaljšanja (mesecev)",
            "odpovedni_rok_mesecev": "Odpovedni rok (mesecev)",
            "datum_odpovedi": "Datum odpovedi",
            "datum_prenehanja": "Datum prenehanja",
            "status": "Pogodbeni status",
            "ima_klavzulo_indeksacije": "Ima klavzulo indeksacije",
            "opis_pogoja_indeksacije": "Opis pogoja indeksacije",
            "tip_pogoja_indeksacije": "Tip pogoja indeksacije",
            "prag_spremembe_odstotek": "Prag spremembe (%)",
        }
        help_texts = {  # noqa: RUF012
            "oznaka_pogodbe": (
                "Iz podpisanega izvoda pogodbe. Izpiše se na računu kot sklic."
            ),
            "datum_prenehanja": "Zadnji dan zaračunavanja pogodbe.",
            "opis_pogoja_indeksacije": "Dokumentarni opis za uporabnika.",
        }
        widgets = {
            name: forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})
            for name in (
                "datum_zacetka", "datum_konca", "datum_odpovedi",
                "datum_prenehanja",
            )
        } | {"opis_pogoja_indeksacije": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        units = Unit.objects.filter(
            aktivna=True, property__aktivna=True, property__company__aktivno=True
        )
        tenants = Tenant.objects.filter(aktiven=True)
        if self.instance.pk:
            units = units | Unit.objects.filter(pk=self.instance.unit_id)
            tenants = tenants | Tenant.objects.filter(pk=self.instance.tenant_id)
        self.fields["unit"].queryset = units.select_related(
            "property__company"
        ).order_by("property__company__naziv", "property__naziv", "oznaka", "pk")
        self.fields["tenant"].queryset = tenants.order_by("naziv", "pk")

    def clean_rok_placila_dni(self):
        value = self.cleaned_data["rok_placila_dni"]
        if self.instance.pk and value is None:
            raise forms.ValidationError(
                "Rok plačila je obvezen pri urejanju obstoječe pogodbe."
            )
        return value


class RecipientContactForm(forms.ModelForm):
    class Meta:
        model = RecipientContact
        fields = ("email", "vloga")
        labels = {"email": "E-pošta", "vloga": "Vloga"}  # noqa: RUF012
        error_messages = {  # noqa: RUF012
            "email": {"invalid": "Vnesite veljaven e-poštni naslov."},
        }


LEASE_ITEM_CREATE_FIELDS = (
    "naziv_postavke", "znesek_neto_mesecno", "vat_regime",
    "referenca_listine", "datum_reference", "datum_zacetka_postavke",
    "datum_prenehanja_postavke", "indeks_ob_osnovi",
    "datum_osnove_indeksacije", "osnovna_najemnina_ob_sklenitvi",
)
LEASE_ITEM_METADATA_FIELDS = (
    "naziv_postavke", "vat_regime", "referenca_listine", "datum_reference",
    "indeks_ob_osnovi", "datum_osnove_indeksacije",
    "osnovna_najemnina_ob_sklenitvi",
)
LEASE_ITEM_LABELS = {
    "naziv_postavke": "Naziv postavke",
    "znesek_neto_mesecno": "Neto mesečni znesek",
    "vat_regime": "DDV režim",
    "referenca_listine": "Referenca listine",
    "datum_reference": "Datum reference",
    "datum_zacetka_postavke": "Začetek veljavnosti",
    "datum_prenehanja_postavke": "Konec veljavnosti",
    "indeks_ob_osnovi": "Izhodiščni indeks",
    "datum_osnove_indeksacije": "Datum osnove indeksacije",
    "osnovna_najemnina_ob_sklenitvi": "Osnovna najemnina ob sklenitvi",
}
LEASE_ITEM_DATE_WIDGETS = {
    name: forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})
    for name in (
        "datum_reference", "datum_zacetka_postavke",
        "datum_prenehanja_postavke", "datum_osnove_indeksacije",
    )
}


class LeaseItemCreateForm(forms.ModelForm):
    vat_regime = VatRegimeChoiceField(
        queryset=VatRegime.objects.none(), label="DDV režim"
    )

    class Meta:
        model = LeaseItem
        fields = LEASE_ITEM_CREATE_FIELDS
        labels = LEASE_ITEM_LABELS
        widgets = LEASE_ITEM_DATE_WIDGETS

    def __init__(self, *args, lease, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.lease = lease
        self.fields["vat_regime"].queryset = VatRegime.objects.filter(
            aktiven=True
        ).order_by("sifra", "pk")


class LeaseItemDetailsForm(forms.ModelForm):
    vat_regime = VatRegimeChoiceField(
        queryset=VatRegime.objects.none(), label="DDV režim"
    )

    class Meta:
        model = LeaseItem
        fields = LEASE_ITEM_METADATA_FIELDS
        labels = LEASE_ITEM_LABELS
        widgets = LEASE_ITEM_DATE_WIDGETS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        regimes = VatRegime.objects.filter(aktiven=True)
        if self.instance.pk:
            regimes = regimes | VatRegime.objects.filter(pk=self.instance.vat_regime_id)
        self.fields["vat_regime"].queryset = regimes.order_by("sifra", "pk")


class LeaseItemCorrectionForm(forms.Form):
    znesek_neto_mesecno = forms.DecimalField(
        max_digits=10, decimal_places=2, label="Neto mesečni znesek"
    )
    datum_zacetka_postavke = forms.DateField(
        required=False, label="Začetek veljavnosti",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    datum_prenehanja_postavke = forms.DateField(
        required=False, label="Konec veljavnosti",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    razlog = forms.CharField(
        label="Razlog spremembe",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Razlog je obvezen, ker se sprememba zapiše v revizijsko sled.",
    )


class VatRegimeForm(forms.ModelForm):
    class Meta:
        model = VatRegime
        fields = (
            "sifra",
            "naziv",
            "nacin_obracuna",
            "stopnja",
            "klavzula",
            "rekapitulacijska_skupina",
            "zahteva_referenco",
            "velja_od",
            "velja_do",
        )
        labels = {  # noqa: RUF012
            "sifra": "Šifra",
            "naziv": "Naziv",
            "nacin_obracuna": "Način obračuna",
            "stopnja": "Stopnja",
            "klavzula": "Klavzula",
            "rekapitulacijska_skupina": "Skupina za rekapitulacijo DDV",
            "zahteva_referenco": "Zahteva referenco",
            "velja_od": "Velja od",
            "velja_do": "Velja do",
        }
        help_texts = {  # noqa: RUF012
            "klavzula": "To je besedilo, ki se po potrebi izpiše na računu.",
            "rekapitulacijska_skupina": (
                "Postavke z isto skupino se na računu seštejejo v eno vrstico."
            ),
            "zahteva_referenco": (
                "Pri uporabi režima bodo zahtevani podatki reference oziroma listine."
            ),
        }
        widgets = {  # noqa: RUF012
            "velja_od": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "velja_do": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "klavzula": forms.Textarea(attrs={"rows": 3}),
        }

    def save(self, commit=True):
        self.instance.eslog_koda = ESLOG_KODE_DDV[
            self.cleaned_data["nacin_obracuna"]
        ]
        return super().save(commit=commit)


class ServiceCatalogItemForm(forms.ModelForm):
    privzeti_ddv_rezim = VatRegimeChoiceField(
        queryset=VatRegime.objects.all(),
        required=False,
        label="Privzeti DDV režim",
    )

    class Meta:
        model = ServiceCatalogItem
        fields = ("naziv", "privzeta_cena", "privzeti_ddv_rezim", "enota_mere")
        labels = {  # noqa: RUF012
            "naziv": "Naziv",
            "privzeta_cena": "Privzeta cena",
            "enota_mere": "Enota mere",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["privzeti_ddv_rezim"].queryset = VatRegime.objects.order_by(
            "-aktiven", "sifra", "pk"
        )


class _KeepExistingFileMixin:
    file_field_name = ""

    def _get_validation_exclusions(self):
        exclude = super()._get_validation_exclusions()
        if self.instance.pk and self.file_field_name not in self.changed_data:
            exclude.add(self.file_field_name)
        return exclude


class CompanyForm(_KeepExistingFileMixin, forms.ModelForm):
    file_field_name = "logotip"

    class Meta:
        model = Company
        fields = (
            "naziv",
            "davcna_stevilka",
            "maticna_stevilka",
            "naslov",
            "drzava_koda",
            "trr",
            "logotip",
            "ddv_zavezanec",
            "email_racunovodja",
            "email_posiljatelj",
            "keychain_kljuc_posiljatelja",
            "rok_placila_dni_privzeto",
        )
        labels = {  # noqa: RUF012
            "naziv": "Naziv",
            "davcna_stevilka": "Davčna številka",
            "maticna_stevilka": "Matična številka",
            "naslov": "Naslov",
            "drzava_koda": "Država",
            "trr": "TRR",
            "logotip": "Logotip",
            "ddv_zavezanec": "DDV zavezanec",
            "email_racunovodja": "E-pošta računovodje",
            "email_posiljatelj": "E-pošta pošiljatelja",
            "keychain_kljuc_posiljatelja": "Ključ pošiljatelja v Keychainu",
            "rok_placila_dni_privzeto": "Privzeti rok plačila (dni)",
        }


class SignatoryForm(_KeepExistingFileMixin, forms.ModelForm):
    file_field_name = "podpis_slika"

    class Meta:
        model = Signatory
        fields = ("ime_priimek", "funkcija", "podpis_slika", "velja_od", "velja_do")
        labels = {  # noqa: RUF012
            "ime_priimek": "Ime in priimek",
            "funkcija": "Funkcija",
            "podpis_slika": "Slika podpisa",
            "velja_od": "Velja od",
            "velja_do": "Velja do",
        }
        widgets = {  # noqa: RUF012
            "velja_od": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "velja_do": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        }


def obdobje_za_mesec(leto, mesec):
    try:
        return (
            date(leto, mesec, 1),
            date(leto, mesec, calendar.monthrange(leto, mesec)[1]),
        )
    except (TypeError, ValueError, OverflowError):
        return None


class IzbiraMesecaForm(forms.Form):
    leto = forms.IntegerField(
        label="Obračunsko leto",
        error_messages={
            "invalid": "Vnesite veljavno obračunsko leto.",
            "required": "Izberite obračunsko leto.",
        },
    )
    mesec = forms.TypedChoiceField(
        label="Obračunski mesec",
        coerce=int,
        choices=(
            (1, "Januar"),
            (2, "Februar"),
            (3, "Marec"),
            (4, "April"),
            (5, "Maj"),
            (6, "Junij"),
            (7, "Julij"),
            (8, "Avgust"),
            (9, "September"),
            (10, "Oktober"),
            (11, "November"),
            (12, "December"),
        ),
        error_messages={
            "invalid_choice": "Izberite veljaven obračunski mesec.",
            "required": "Izberite obračunski mesec.",
        },
    )
    datum_izdaje = forms.DateField(
        label="Datum izdaje",
        input_formats=("%Y-%m-%d",),
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        error_messages={
            "invalid": "Vnesite veljaven datum izdaje.",
            "required": "Izberite datum izdaje.",
        },
    )

    def __init__(self, *args, **kwargs):
        danes = timezone.localdate()
        kwargs.setdefault(
            "initial",
            {
                "leto": danes.year,
                "mesec": danes.month,
                "datum_izdaje": danes.replace(day=1),
            },
        )
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        leto = cleaned_data.get("leto")
        mesec = cleaned_data.get("mesec")
        if leto is None or mesec is None:
            return cleaned_data

        obdobje = obdobje_za_mesec(leto, mesec)
        if obdobje is None:
            self.add_error("leto", "Vnesite leto, ki ga koledar podpira.")
        else:
            cleaned_data["obdobje_od"], cleaned_data["obdobje_do"] = obdobje

        return cleaned_data
