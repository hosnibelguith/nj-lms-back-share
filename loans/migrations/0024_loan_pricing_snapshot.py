from django.db import migrations, models


def backfill_pricing_snapshot(apps, schema_editor):
    Loan = apps.get_model("loans", "Loan")
    db_alias = schema_editor.connection.alias

    loans = (
        Loan.objects.using(db_alias)
        .select_related("formula")
        .filter(formula__isnull=False)
        .filter(
            models.Q(pricing_brokerage_percent__isnull=True)
            | models.Q(pricing_interest_percent__isnull=True)
            | models.Q(pricing_number_of_payments__isnull=True)
            | models.Q(pricing_frequency_days__isnull=True)
        )
    )
    for loan in loans.iterator():
        formula = loan.formula
        loan.pricing_brokerage_percent = formula.brokerage_percent
        loan.pricing_interest_percent = formula.repayment_percent
        loan.pricing_number_of_payments = formula.default_number_of_payments
        loan.pricing_frequency_days = formula.default_frequency_days
        loan.save(
            using=db_alias,
            update_fields=[
                "pricing_brokerage_percent",
                "pricing_interest_percent",
                "pricing_number_of_payments",
                "pricing_frequency_days",
            ],
        )


class Migration(migrations.Migration):

    dependencies = [
        ("loans", "0023_loanformula_lender"),
    ]

    operations = [
        migrations.AddField(
            model_name="loan",
            name="pricing_brokerage_percent",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Brokerage percentage snapshotted when this loan was priced.",
                max_digits=6,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="loan",
            name="pricing_interest_percent",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Interest percentage snapshotted when this loan was priced.",
                max_digits=6,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="loan",
            name="pricing_number_of_payments",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Payment count snapshotted when this loan was priced.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="loan",
            name="pricing_frequency_days",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Payment frequency snapshotted when this loan was priced.",
                null=True,
            ),
        ),
        migrations.RunPython(backfill_pricing_snapshot, migrations.RunPython.noop),
    ]
