from django.shortcuts import render
from django.views.decorators.http import require_GET

from companies.models import Company
from contracts.models import Lease, Tenant
from invoicing.models import Invoice


@require_GET
def dashboard(request):
    counts = {
        "podjetja": Company.objects.filter(aktivno=True).count(),
        "najemniki": Tenant.objects.filter(aktiven=True).count(),
        "pogodbe": Lease.objects.filter(
            status=Lease.Status.AKTIVNA,
            arhivirana=False,
        ).count(),
        "izdani_racuni": Invoice.objects.filter(stevilka__isnull=False)
        .exclude(stevilka="")
        .count(),
    }
    return render(request, "ui/dashboard.html", {"counts": counts})
