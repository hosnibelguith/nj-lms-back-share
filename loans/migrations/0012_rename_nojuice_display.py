from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("loans", "0011_zumrails_idempotency_constraints"),
    ]

    operations = [
        migrations.AlterField(
            model_name="loan",
            name="type",
            field=models.CharField(
                choices=[("nojuice", "MohawkLoans"), ("payday", "Payday")],
                default="nojuice",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="loanformula",
            name="loan_type",
            field=models.CharField(
                choices=[("nojuice", "MohawkLoans"), ("payday", "Payday")],
                default="nojuice",
                max_length=20,
            ),
        ),
    ]
