from datetime import date

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from contracts.models import Lease, LeaseRenewalEvent
from core.models import AuditLogEntry
from tests.contracts.test_renewals import automatic_lease


@pytest.mark.django_db(transaction=True)
def test_additive_migration_never_backfills_historical_renewals():
    lease = automatic_lease(datum_konca=date(2024, 1, 31))
    original = Lease.objects.filter(pk=lease.pk).values().get()
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    old = [(app, "0009_adjustment_evidence") if app == "contracts" else (app, name) for app, name in latest]
    try:
        MigrationExecutor(connection).migrate(old)
        MigrationExecutor(connection).migrate(latest)
        assert Lease.objects.filter(pk=lease.pk).values().get() == original
        assert not LeaseRenewalEvent.objects.exists()
        assert not AuditLogEntry.objects.exists()
    finally:
        MigrationExecutor(connection).migrate(latest)
