from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0014_customer_ibv_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="ibv_refill_requested",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text="Staff asked the client to complete a new IBV in the portal.",
            ),
        ),
    ]
