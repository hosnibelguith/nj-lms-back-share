from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("loans", "0021_early_renewal_and_customer_documents"),
    ]

    operations = [
        migrations.AlterField(
            model_name="loan",
            name="status",
            field=models.CharField(
                choices=[
                    ("ibv_pending", "IBV Pending"),
                    ("pending", "Pending Human Decision"),
                    ("pending_signature", "Pending Signature"),
                    ("pending_id", "Pending ID"),
                    ("human_declined", "Human Declined"),
                    ("expired", "Expired"),
                    ("pending_funding", "Pending Funding"),
                    ("active", "Active"),
                    ("paid_off", "Paid Off"),
                    ("defaulted", "In Collections"),
                    ("stopped", "Stopped"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]
