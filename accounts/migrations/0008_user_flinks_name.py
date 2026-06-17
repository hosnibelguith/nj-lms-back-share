from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0007_alter_customer_status_default"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="flinks_name",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
