import calendar
from datetime import date

from django import forms
from django.utils import timezone


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
