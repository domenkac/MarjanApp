from django.db import models


class SetupNavigation(models.Model):
    """Device-local navigation only; requirements are never cached here."""

    step = models.CharField(max_length=16, default="company")
    company = models.ForeignKey(
        "companies.Company", null=True, on_delete=models.SET_NULL
    )
    property = models.ForeignKey(
        "contracts.Property", null=True, on_delete=models.SET_NULL
    )
    unit = models.ForeignKey("contracts.Unit", null=True, on_delete=models.SET_NULL)
    tenant = models.ForeignKey("contracts.Tenant", null=True, on_delete=models.SET_NULL)
    lease = models.ForeignKey("contracts.Lease", null=True, on_delete=models.SET_NULL)

    class Meta:
        constraints = [  # noqa: RUF012
            models.CheckConstraint(
                condition=models.Q(id=1), name="setup_navigation_singleton"
            ),
        ]
