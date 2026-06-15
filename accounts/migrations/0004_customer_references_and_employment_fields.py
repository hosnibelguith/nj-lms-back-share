from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_customer_phone_verified_customer_phone_verified_at_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="job_place_name",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="customer",
            name="supervisor_name",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="customer",
            name="supervisor_phone",
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name="customer",
            name="reference_1_name",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="customer",
            name="reference_1_phone",
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name="customer",
            name="reference_2_name",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="customer",
            name="reference_2_phone",
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
    ]
