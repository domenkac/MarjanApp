"""Root routing uses audited completion, independently of current issuer readiness."""

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client
from django.utils import timezone

from companies.models import InitialSetupCompletion, NumberSequence
from companies.readiness import complete_initial_setup, initial_setup_completed
from companies.services import dodeli_naslednjo_stevilko
from core.models import AuditLogEntry
from invoicing.models import Invoice
from tests.companies.test_setup_readiness import (
    five_issuers as five_issuers,  # noqa: PLC0414
)
from tests.financial_fixtures import save_financial_fixture
from tests.ui.test_setup import URL, select_chain
from tests.ui.test_setup import setup_data as setup_data  # noqa: PLC0414
from ui.models import SetupNavigation

pytestmark = pytest.mark.django_db(transaction=True)


def restarted_root():
    """Read the committed test DB from a fresh interpreter and WSGI application."""
    database = Path(connection.settings_dict["NAME"]).resolve()
    assert database.is_file(), "Restart verification requires a file-based test DB"
    script = """
import json
from pathlib import Path
import sys

database = Path(sys.argv[1]).resolve()
def guard(event, args):
    if event == 'sqlite3.connect' and Path(args[0]).resolve() != database:
        raise RuntimeError('Restart attempted to open a different database')
sys.addaudithook(guard)

from django.conf import settings
settings.DATABASES = {'default': {
    'ENGINE': 'django.db.backends.sqlite3', 'NAME': database,
    'OPTIONS': {'transaction_mode': 'IMMEDIATE'}, 'TEST': {'NAME': database},
}}
settings.MEDIA_ROOT = sys.argv[2]
settings.RENDERING_ROOT = sys.argv[3]
from django.core.wsgi import get_wsgi_application
get_wsgi_application()
from django.test import Client
from django.test.utils import setup_test_environment
setup_test_environment()
response = Client(HTTP_HOST='127.0.0.1').get('/', follow=True)
state = response.context.get('state')
print(json.dumps({
    'status': response.status_code, 'redirects': response.redirect_chain,
    'step': state.step if state else None,
    'lease_id': state.lease_id if state else None,
    'dashboard': any(t.name == 'ui/dashboard.html' for t in response.templates),
}))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(database),
         str(settings.MEDIA_ROOT), str(settings.RENDERING_ROOT)],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(result.stdout)


def test_fresh_root_redirects_to_wizard_without_completion(client):
    response = client.get("/", follow=True)
    assert response.redirect_chain == [(URL, 302)]
    assert response.context["step"] == "company"
    assert response.context["checked_count"] == 0
    assert not initial_setup_completed()
    assert not InitialSetupCompletion.objects.exists()
    assert not Invoice.objects.exists()


def test_incomplete_root_resumes_saved_navigation_after_restart(client, setup_data):
    select_chain(client, setup_data)
    client.post(URL, {"action": "next"})
    response = Client().get("/", follow=True)
    assert response.redirect_chain == [(URL, 302)]
    assert response.context["step"] == "issuer"
    assert response.context["state"].lease_id == setup_data.lease.pk
    assert restarted_root() == {
        "status": 200, "redirects": [[URL, 302]], "step": "issuer",
        "lease_id": setup_data.lease.pk, "dashboard": False,
    }


def test_finish_redirects_to_dashboard_and_survives_restart(client, five_issuers):
    response = client.post(URL, {"action": "finish"}, follow=True)
    assert response.redirect_chain == [("/", 302)]
    assert response.templates[0].name == "ui/dashboard.html"
    assert initial_setup_completed()
    for _ in range(2):
        response = Client().get("/")
        assert response.status_code == 200
        assert response.templates[0].name == "ui/dashboard.html"
    assert restarted_root() == {
        "status": 200, "redirects": [], "step": None,
        "lease_id": None, "dashboard": True,
    }
    assert InitialSetupCompletion.objects.count() == 1
    assert AuditLogEntry.objects.filter(polje="initial_setup").count() == 1
    assert not Invoice.objects.exists()


@pytest.mark.parametrize("changed", ["vat", "lease", "company"])
def test_later_changes_keep_dashboard_and_issuance_protection(client, five_issuers, changed):
    data, _ = five_issuers
    completion = complete_initial_setup()
    record, field, value = {
        "vat": (data.regime, "stopnja", Decimal("9.50")),
        "lease": (data.lease, "rok_placila_dni", 20),
        "company": (data.company, "naziv", "Spremenjeno podjetje"),
    }[changed]
    setattr(record, field, value)
    save_financial_fixture(record, update_fields=[field])
    response = Client().get("/")
    assert response.status_code == 200
    assert response.context["confirmation_tasks"]
    assert initial_setup_completed()
    assert InitialSetupCompletion.objects.get().evidence == completion.evidence
    with pytest.raises(ValidationError, match="zastarela"):
        dodeli_naslednjo_stevilko(data.company, "NP", timezone.localdate())


@pytest.mark.parametrize("invalid", ["missing", "forged", "no_audit", "tampered"])
def test_invalid_completion_blocks_root_and_issuance(client, five_issuers, invalid, monkeypatch):
    data, _ = five_issuers
    if invalid == "forged":
        InitialSetupCompletion.objects.create(pk=1, evidence={"complete": True})
    elif invalid == "no_audit":
        # Simulate missing audit evidence without deleting immutable history.
        monkeypatch.setattr("companies.readiness._audit", lambda *_: None)
        complete_initial_setup()
    elif invalid == "tampered":
        complete_initial_setup()
        InitialSetupCompletion.objects.update(evidence={"complete": True})
    before = list(NumberSequence.objects.values())
    assert not initial_setup_completed()
    assert client.get("/").url == URL
    for tip in ("NP", "ST"):
        with pytest.raises(ValidationError, match="Začetna nastavitev"):
            dodeli_naslednjo_stevilko(data.company, tip, timezone.localdate())
    assert list(NumberSequence.objects.values()) == before
    assert not Invoice.objects.exists()


def test_completed_wizard_remains_accessible_from_navigation(client, five_issuers):
    complete_initial_setup()
    assert f'href="{URL}"' in client.get("/nastavitve/").content.decode()
    response = client.get(URL)
    assert response.status_code == 200
    assert response.context["completion"].pk == 1
    assert SetupNavigation.objects.count() == 1
