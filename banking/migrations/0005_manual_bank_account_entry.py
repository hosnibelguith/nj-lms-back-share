from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("banking", "0004_mohawk_banking_analysis_webhook"),
    ]

    operations = [
        migrations.AlterField(
            model_name="bankconnection",
            name="provider",
            field=models.CharField(
                choices=[("flinks", "Flinks"), ("manual", "Manual / Void Cheque")],
                default="flinks",
                max_length=50,
            ),
        ),
        migrations.AddField(
            model_name="bankaccount",
            name="is_manual_entry",
            field=models.BooleanField(default=False),
        ),
    ]
