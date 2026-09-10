import django.db.models.deletion
from django.db import migrations, models


def assign_default_lender(apps, schema_editor):
    Lender = apps.get_model("accounts", "Lender")
    LoanFormula = apps.get_model("loans", "LoanFormula")
    lender = Lender.objects.filter(slug="mohawkloans").first()
    if lender is None:
        return
    LoanFormula.objects.filter(lender__isnull=True).update(lender=lender)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0016_lender_user_customer"),
        ("loans", "0022_loan_pending_id_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="loanformula",
            name="lender",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="loan_formulas", to="accounts.lender"),
        ),
        migrations.RunPython(assign_default_lender, migrations.RunPython.noop),
    ]
