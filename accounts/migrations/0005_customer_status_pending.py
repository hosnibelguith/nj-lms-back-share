from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_customer_references_and_employment_fields'),
    ]

    operations = [
        migrations.AlterField(
            model_name='customer',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'),
                    ('active', 'Active'),
                    ('inactive', 'Inactive'),
                    ('blocked', 'Blocked'),
                ],
                db_index=True,
                default='pending',
                max_length=20,
            ),
        ),
    ]
