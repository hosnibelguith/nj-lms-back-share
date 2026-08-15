from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("banking", "0005_manual_bank_account_entry"),
    ]

    operations = [
        migrations.AddField(
            model_name="bankconnection",
            name="attempted_syncs",
            field=models.PositiveIntegerField(
                default=0,
                help_text="How many Authorize/GetAccountsDetail pulls have been tried for this LoginId.",
            ),
        ),
    ]
