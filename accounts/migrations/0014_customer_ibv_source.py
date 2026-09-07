from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0013_early_renewal_and_customer_documents"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="ibv_source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Not set"),
                    ("flinks", "Flinks"),
                    ("syncdata", "SyncData"),
                ],
                default="",
                help_text="How IBV was completed: Flinks in this LMS, or marked received from SyncData.",
                max_length=20,
            ),
        ),
    ]
