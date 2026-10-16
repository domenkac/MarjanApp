from django import forms

from catalogs.models import ServiceCatalogItem, VatRegime
from companies.models import Company
from contracts.models import Tenant

from .forms import CompanyChoiceField, TenantChoiceField, VatRegimeChoiceField


class STListFilterForm(forms.Form):
    company = CompanyChoiceField(
        label="Izdajatelj", required=False, empty_label="Vsa podjetja",
        queryset=Company.objects.order_by("naziv", "pk"),
    )


class ServiceChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        regime = obj.privzeti_ddv_rezim
        default = f"DDV: {regime.sifra}" if regime else "izberite DDV režim"
        return f"{obj.naziv} — {obj.enota_mere} — {default}"


class STDraftForm(forms.Form):
    company = CompanyChoiceField(
        label="Izdajatelj", queryset=Company.objects.filter(aktivno=True).order_by("naziv", "pk"),
    )
    datum_izdaje = forms.DateField(label="Datum izdaje")
    obdobje_od = forms.DateField(label="Obdobje storitve od")
    obdobje_do = forms.DateField(label="Obdobje storitve do")
    prejemnik_nacin = forms.ChoiceField(
        label="Prejemnik", choices=[("obstojeci", "Obstoječi prejemnik"), ("nov", "Nov prejemnik")],
        initial="obstojeci",
        help_text="Za nov vnos izpraznite izbor obstoječega prejemnika in izpolnite podatke spodaj.",
    )
    prejemnik = TenantChoiceField(
        label="Obstoječi prejemnik", required=False,
        queryset=Tenant.objects.filter(aktiven=True).order_by("naziv", "pk"),
    )
    novi_tip = forms.ChoiceField(
        label="Nov prejemnik: tip", required=False, choices=[("", "---------"), *Tenant.Tip.choices],
    )
    novi_naziv = forms.CharField(label="Nov prejemnik: naziv", required=False)
    novi_naslov = forms.CharField(label="Nov prejemnik: naslov", required=False)
    novi_drzava_koda = forms.CharField(
        label="Nov prejemnik: koda države", required=False, max_length=2, initial="SI",
    )
    novi_davcna_stevilka = forms.CharField(label="Nov prejemnik: davčna številka", required=False, max_length=70)
    novi_maticna_stevilka = forms.CharField(label="Nov prejemnik: matična številka", required=False, max_length=70)
    novi_ddv_identifikacijska_stevilka = forms.CharField(
        label="Nov prejemnik: DDV identifikacijska številka", required=False, max_length=70,
    )
    novi_identificiran_za_ddv = forms.BooleanField(label="Nov prejemnik: identificiran za DDV", required=False)
    novi_ddv_zavezanec_s_polnim_odbitkom = forms.BooleanField(
        label="Nov prejemnik: DDV zavezanec s polnim odbitkom", required=False,
    )
    storitev = ServiceChoiceField(
        label="Storitev", required=False, empty_label="Drugo — vpiši ročno",
        queryset=ServiceCatalogItem.objects.filter(aktivna=True).select_related(
            "privzeti_ddv_rezim",
        ).order_by("naziv", "pk"),
        help_text="Prazni opis, enota in DDV režim uporabijo privzete vrednosti izbrane storitve.",
    )
    opis = forms.CharField(label="Opis storitve", required=False, widget=forms.Textarea(attrs={"rows": 3}))
    enota_mere = forms.CharField(label="Enota mere", required=False)
    vat_regime = VatRegimeChoiceField(
        label="DDV režim", required=False, empty_label="Privzeti režim izbrane storitve",
        queryset=VatRegime.objects.filter(aktiven=True).order_by("sifra", "pk"),
    )
    bruto = forms.DecimalField(
        label="Bruto znesek (EUR)", max_digits=10, decimal_places=2, localize=True,
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
        help_text="Vnesite skupni bruto znesek storitve, večji od nič. Največ dve decimalni mesti.",
    )
    referenca_listine = forms.CharField(
        label="Referenca listine", required=False,
        help_text="Referenca in njen datum sta obvezna, kadar ju zahteva izbrani DDV režim.",
    )
    datum_reference = forms.DateField(label="Datum reference", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field, forms.DateField):
                field.input_formats = ["%Y-%m-%d"]
                field.widget = forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})
        if self.is_bound and self.data.get("prejemnik_nacin") == "nov":
            for name in ("novi_tip", "novi_naziv", "novi_naslov", "novi_drzava_koda"):
                self.fields[name].required = True

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("prejemnik_nacin") == "obstojeci" and not cleaned.get("prejemnik"):
            self.add_error("prejemnik", "Izberite obstoječega prejemnika.")
        if cleaned.get("prejemnik_nacin") == "nov" and cleaned.get("prejemnik"):
            self.add_error("prejemnik", "Za nov vnos izpraznite izbor obstoječega prejemnika.")
        return cleaned

    def service_arguments(self):
        data = self.cleaned_data
        arguments = {name: data[name] for name in (
            "company", "datum_izdaje", "obdobje_od", "obdobje_do", "bruto", "storitev",
            "vat_regime", "opis", "referenca_listine", "datum_reference",
        )}
        arguments["enota_mere"] = data["enota_mere"] or None
        if data["prejemnik_nacin"] == "nov":
            arguments["prejemnik_podatki"] = {
                name.removeprefix("novi_"): value
                for name, value in data.items() if name.startswith("novi_")
            }
        else:
            arguments["prejemnik"] = data["prejemnik"]
        return arguments
