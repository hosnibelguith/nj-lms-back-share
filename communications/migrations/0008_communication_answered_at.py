from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("communications", "0007_seed_manual_staff_templates"),
    ]

    operations = [
        migrations.AddField(
            model_name="communication",
            name="answered_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
