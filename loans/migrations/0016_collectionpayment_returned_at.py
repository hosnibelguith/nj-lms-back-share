from django.db import migrations, models
from django.db.models import F


def backfill_returned_at(apps, schema_editor):
    CollectionPayment = apps.get_model("loans", "CollectionPayment")
    CollectionPayment.objects.filter(
        status__in=["failed", "returned", "rejected"],
        returned_at__isnull=True,
    ).update(returned_at=F("updated_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("loans", "0015_add_expired_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="collectionpayment",
            name="returned_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(backfill_returned_at, migrations.RunPython.noop),
    ]
