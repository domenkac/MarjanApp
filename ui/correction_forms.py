from django import forms
from django.db.models import Q

from catalogs.models import VatRegime
from invoicing.corrections import VAT_FIELDS
from invoicing.models import Invoice


def source_invoices(company_id):
    return Invoice.objects.filter(
        company_id=company_id, document_type__in=["NP", "ST"],
        povezan_racun__isnull=True,
    ).exclude(Q(stevilka__isnull=True) | Q(stevilka="") | Q(status="OSNUTEK"))


class OriginalChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.stevilka} · {obj.prejemnik_naziv} · {obj.datum_izdaje:%d. %m. %Y}"


class CorrectionDataForm(forms.Form):
    datum_izdaje = forms.DateField(
        label="Datum izdaje", input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    razlog = forms.CharField(
        label="Razlog popravka", widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Opišite konkretno napako oziroma razlog popravka.",
    )

    def clean_razlog(self):
        value = self.cleaned_data["razlog"]
        if not any(char.isalpha() for char in value):
            raise forms.ValidationError("Vpišite smiseln opis razloga popravka.")
        return value


class CorrectionDraftForm(CorrectionDataForm):
    original = OriginalChoiceField(label="Izvirni izdani račun", queryset=Invoice.objects.none())
    document_type = forms.ChoiceField(label="Vrsta popravka", choices=[
        ("STORNO", "Storno"), ("DOBROPIS", "Dobropis"),
    ])
    field_order = ("original", "document_type", "datum_izdaje", "razlog")

    def __init__(self, *args, company_id, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["original"].queryset = source_invoices(company_id).order_by("-datum_izdaje", "-pk")


class CorrectionLineForm(forms.Form):
    source_token = forms.CharField(required=False, widget=forms.HiddenInput)
    opis = forms.CharField(label="Opis", widget=forms.Textarea(attrs={"rows": 2}))
    enota_mere = forms.CharField(label="Enota mere", required=False)
    osnova = forms.DecimalField(label="Osnova (EUR)", max_digits=10, decimal_places=2, localize=True)
    ddv_stopnja = forms.DecimalField(label="Stopnja DDV (%)", max_digits=5, decimal_places=2, localize=True)
    ddv_znesek = forms.DecimalField(label="DDV (EUR)", max_digits=10, decimal_places=2, localize=True)
    skupaj = forms.DecimalField(label="Skupaj (EUR)", max_digits=10, decimal_places=2, localize=True)
    ddv_nacin_obracuna = forms.ChoiceField(label="Način obračuna DDV", choices=VatRegime.NacinObracuna.choices)
    ddv_eslog_koda = forms.CharField(label="eSLOG koda DDV")
    ddv_rekap_skupina = forms.CharField(label="Skupina DDV rekapitulacije")
    ddv_klavzula = forms.CharField(label="DDV klavzula", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    referenca_listine = forms.CharField(label="Referenca listine", required=False)
    datum_reference = forms.DateField(
        label="Datum reference", required=False, input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    razlog_rocnega_popravka = forms.CharField(label="Razlog ročnega popravka postavke", required=False)

    def __init__(self, *args, historical_source=None, **kwargs):
        super().__init__(*args, **kwargs)
        if historical_source is not None:
            for name in VAT_FIELDS:
                self.fields[name].disabled = True
                self.initial[name] = getattr(historical_source, name)

    @classmethod
    def initial_from_line(cls, line, *, source=False):
        # Reuse editable historical VAT/reference snapshots. Credit amounts remain
        # manual; immutable regime metadata is never guessed from a catalog.
        return {
            name: getattr(line, name) for name in cls.base_fields if name != "source_token"
            and (not source or name not in {"osnova", "ddv_znesek", "skupaj", "razlog_rocnega_popravka"})
        }

    def add_service_error(self, error):
        if hasattr(error, "message_dict"):
            for field, messages in error.message_dict.items():
                for message in messages:
                    self.add_error(field if field in self.fields else None, message)
        else:
            self.add_error(None, str(error))


class CorrectionReviewForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)
    reviewed = forms.BooleanField(label="Posamično sem pregledal izvirnik, razlog, vse postavke in skupne zneske.")


class CorrectionConfirmForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)
    confirm_issue = forms.BooleanField(label="Izrecno potrjujem dokončno izdajo in dodelitev uradne številke.")
