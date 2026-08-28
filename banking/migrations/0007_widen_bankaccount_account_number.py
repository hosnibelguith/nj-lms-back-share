from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("banking", "0006_bankconnection_attempted_syncs"),
    ]

    operations = [
        migrations.AlterField(
            model_name="bankaccount",
            name="account_number",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
    ]
