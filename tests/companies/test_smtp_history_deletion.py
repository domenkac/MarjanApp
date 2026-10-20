"""Company deletion respects retained SMTP test history without contacting SMTP."""

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from companies.models import Company
from companies.services import (
    fizicno_izbrisi_poslovni_zapis,
    odloci_o_fizicnem_izbrisu,
)
from core.models import AuditLogEntry
from delivery.models import SmtpVerification

pytestmark = pytest.mark.django_db


@pytest.fixture
def company():
    return Company.objects.create(
        naziv="Unused company", davcna_stevilka="15012557",
        maticna_stevilka="1234567000", naslov="Glavna 1", drzava_koda="SI",
        trr="SI56191000000123438", logotip="synthetic-logo.png",
        email_posiljatelj="sender@example.com", email_racunovodja="accountant@example.com",
        keychain_kljuc_posiljatelja="synthetic-reference",
    )


@pytest.mark.parametrize("successful", [True, False])
def test_smtp_history_blocks_decision_and_physical_deletion(company, successful):
    SmtpVerification.objects.create(
        company=company, sender=company.email_posiljatelj, fingerprint="a" * 64,
        verified_at=timezone.now() if successful else None,
        result_code="SMTP_CONNECTION_OK" if successful else "SMTP_TEST_FAILED",
    )
    history = list(SmtpVerification.objects.values())
    company_id = company.pk
    with pytest.raises(ProtectedError):
        company.delete()

    decision = odloci_o_fizicnem_izbrisu(company, izrecna_potrditev=True)
    assert not decision.dovoljeno
    assert decision.mozno_arhiviranje
    assert len(decision.razlogi) == 1
    reason = decision.razlogi[0]
    assert reason.koda == "POSLOVNA_ZGODOVINA"
    assert reason.tip_povezave == "SmtpVerification"
    assert reason.stevilo_povezav == 1
    assert reason.sporocilo == (
        "Ohranjena zgodovina preverjanj SMTP povezave preprečuje fizični izbris."
    )
    assert fizicno_izbrisi_poslovni_zapis(company, izrecna_potrditev=True) == decision
    assert Company.objects.filter(pk=company_id).exists()
    assert list(SmtpVerification.objects.values()) == history
    assert not AuditLogEntry.objects.filter(dejanje="FIZICNI_IZBRIS").exists()


@pytest.mark.parametrize("unrelated_history", [None, "other_company", "global_summary"])
def test_unused_company_without_own_smtp_history_remains_deletable(company, unrelated_history):
    if unrelated_history:
        owner = None
        if unrelated_history == "other_company":
            owner = Company.objects.create(naziv="Other issuer", davcna_stevilka="12345679")
        SmtpVerification.objects.create(
            company=owner, sender="other@example.com", fingerprint="b" * 64,
            result_code="SMTP_TEST_FAILED",
        )
    history = list(SmtpVerification.objects.values())
    company_id = company.pk
    assert odloci_o_fizicnem_izbrisu(company, izrecna_potrditev=True).dovoljeno
    assert fizicno_izbrisi_poslovni_zapis(company, izrecna_potrditev=True).dovoljeno
    assert not Company.objects.filter(pk=company_id).exists()
    assert list(SmtpVerification.objects.values()) == history
    assert AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(Company),
        object_id=company_id, dejanje="FIZICNI_IZBRIS",
    ).count() == 1
