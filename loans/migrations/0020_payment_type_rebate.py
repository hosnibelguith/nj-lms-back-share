from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("loans", "0019_stopped_loan_unscheduled_payments"),
    ]

    operations = [
        migrations.AlterField(
            model_name="payment",
            name="type",
            field=models.CharField(
                choices=[
                    ("scheduled", "Scheduled PAD"),
                    ("manual", "Manual Payment"),
                    ("etransfer", "e-Transfer Received"),
                    ("rebate", "Rebate"),
                ],
                default="scheduled",
                max_length=20,
            ),
        ),
    ]
