import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("contracts", "0001_initial"),
        ("invoicing", "0009_invoice_dostavni_kontakti_snapshot"),
    ]

    operations = [
        migrations.CreateModel(
            name="MonthlyCandidateReview",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("candidate_fingerprint", models.CharField(max_length=64)),
                ("selected", models.BooleanField()),
                ("requires_confirmation", models.BooleanField()),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("non_issue_reason", models.TextField(blank=True, default="")),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="candidate_reviews", to="invoicing.monthlybatch")),
                ("lease", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="contracts.lease")),
            ],
        ),
        migrations.AddConstraint(
            model_name="monthlycandidatereview",
            constraint=models.UniqueConstraint(fields=("batch", "lease", "candidate_fingerprint"), name="uniq_monthly_candidate_review_version"),
        ),
    ]
