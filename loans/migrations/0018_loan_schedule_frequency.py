from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("loans", "0017_bankholiday_payment_original_date"),
    ]

    operations = [
        migrations.AddField(
            model_name="loan",
            name="schedule_frequency",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Staff-selected cadence: weekly, bi-weekly, monthly, twice-monthly",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="loan",
            name="twice_monthly_day_1",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="loan",
            name="twice_monthly_day_2",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
    ]
