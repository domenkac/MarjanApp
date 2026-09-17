import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError

from contracts.models import LeaseItem
from core.models import AuditLogEntry


@pytest.fixture
def content_type():
    return ContentType.objects.get_for_model(LeaseItem)


@pytest.mark.django_db
def test_polja_privzeto_dejanje_in_indeksi(content_type):
    assert {field.name for field in AuditLogEntry._meta.concrete_fields} == {
        "id", "content_type", "object_id", "dejanje", "polje",
        "stara_vrednost", "nova_vrednost", "razlog", "created_at",
    }
    assert set(AuditLogEntry.Dejanje.values) == {"SPREMEMBA", "FIZICNI_IZBRIS"}
    assert {tuple(index.fields) for index in AuditLogEntry._meta.indexes} == {
        ("content_type", "object_id", "polje", "created_at"),
        ("content_type", "object_id", "dejanje", "created_at"),
    }
    entry = AuditLogEntry(
        content_type=content_type, object_id=42, razlog="Popravek najemnine"
    )
    entry.full_clean()
    entry.save()
    entry.refresh_from_db()
    assert entry.dejanje == AuditLogEntry.Dejanje.SPREMEMBA
    assert entry.created_at is not None
    assert entry.polje == entry.stara_vrednost == entry.nova_vrednost == ""


@pytest.mark.django_db
@pytest.mark.parametrize("dejanje", AuditLogEntry.Dejanje.values)
def test_razlog_je_obvezen_za_obe_dejanji(content_type, dejanje):
    entry = AuditLogEntry(
        content_type=content_type,
        object_id=42,
        dejanje=dejanje,
        razlog="  ",
    )
    with pytest.raises(ValidationError) as error:
        entry.full_clean()
    assert "razlog" in error.value.message_dict

    entry.razlog = "Potrjen popravek"
    entry.full_clean()


@pytest.mark.django_db
def test_neveljavno_dejanje_in_negativen_object_id(content_type):
    entry = AuditLogEntry(
        content_type=content_type, object_id=-1, dejanje="DRUGO", razlog="Popravek"
    )
    with pytest.raises(ValidationError) as error:
        entry.full_clean()
    assert {"dejanje", "object_id"} <= set(error.value.message_dict)


@pytest.mark.django_db
@pytest.mark.parametrize("polje", ["polje", "stara_vrednost", "nova_vrednost"])
def test_fizicni_izbris_ne_hrani_poslovnih_vrednosti(content_type, polje):
    entry = AuditLogEntry(
        content_type=content_type,
        object_id=42,
        dejanje=AuditLogEntry.Dejanje.FIZICNI_IZBRIS,
        razlog="Uporabnik je potrdil fizični izbris neuporabljenega zapisa",
    )
    entry.full_clean()
    setattr(entry, polje, "vrednost")
    with pytest.raises(ValidationError) as error:
        entry.full_clean()
    assert polje in error.value.message_dict


@pytest.mark.django_db
def test_content_type_fk_je_protected(content_type):
    AuditLogEntry.objects.create(
        content_type=content_type, object_id=42, razlog="Popravek"
    )
    with pytest.raises(ProtectedError):
        content_type.delete()
