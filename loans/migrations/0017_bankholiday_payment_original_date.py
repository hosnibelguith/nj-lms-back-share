from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("loans", "0016_collectionpayment_returned_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="original_date",
            field=models.DateField(
                blank=True,
                help_text="Unadjusted scheduled date before weekend/holiday shift",
                null=True,
            ),
        ),
        migrations.CreateModel(
            name="BankHoliday",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("date", models.DateField(unique=True)),
                ("name", models.CharField(max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_bank_holidays",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "loans_bank_holiday",
                "ordering": ["date"],
            },
        ),
    ]
