from decimal import Decimal

import django.db.models.deletion
import uuid
from django.db import migrations, models


def seed_default_lender(apps, schema_editor):
    Lender = apps.get_model("accounts", "Lender")
    User = apps.get_model("accounts", "User")
    Customer = apps.get_model("accounts", "Customer")
    lender, _created = Lender.objects.get_or_create(
        slug="mohawkloans",
        defaults={
            "name": "MohawkLoans",
            "primary_domain": "mohawkloans.com",
            "nsf_fee_amount": Decimal("50.00"),
            "brokerage_percent": Decimal("70.00"),
            "interest_percent": Decimal("35.00"),
            "is_active": True,
        },
    )
    User.objects.filter(lender__isnull=True).update(lender=lender)
    Customer.objects.filter(lender__isnull=True).update(lender=lender)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0015_customer_ibv_refill_requested"),
    ]

    operations = [
        migrations.CreateModel(
            name="Lender",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=255)),
                ("slug", models.SlugField(db_index=True, max_length=80, unique=True)),
                ("primary_domain", models.CharField(blank=True, max_length=255, null=True, unique=True)),
                ("logo_url", models.URLField(blank=True, default="")),
                ("primary_color", models.CharField(blank=True, default="", max_length=32)),
                ("secondary_color", models.CharField(blank=True, default="", max_length=32)),
                ("nsf_fee_amount", models.DecimalField(decimal_places=2, default=Decimal("50.00"), max_digits=10)),
                ("brokerage_percent", models.DecimalField(decimal_places=2, default=Decimal("70.00"), max_digits=6)),
                ("interest_percent", models.DecimalField(decimal_places=2, default=Decimal("35.00"), max_digits=6)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "accounts_lender",
                "ordering": ["name"],
            },
        ),
        migrations.AddField(
            model_name="user",
            name="lender",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="users", to="accounts.lender"),
        ),
        migrations.AddField(
            model_name="customer",
            name="lender",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="customers", to="accounts.lender"),
        ),
        migrations.RunPython(seed_default_lender, migrations.RunPython.noop),
    ]
