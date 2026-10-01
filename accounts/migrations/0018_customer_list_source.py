from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('accounts', '0017_customer_alphaloans_source')]

    operations = [
        migrations.AlterField(
            model_name='customer',
            name='source',
            field=models.CharField(
                choices=[('organic', 'Organic'), ('arrive', 'Arrive'),
                         ('alphaloans', 'Alphaloans'), ('list', 'List')],
                db_index=True, default='organic', max_length=20,
            ),
        ),
    ]
