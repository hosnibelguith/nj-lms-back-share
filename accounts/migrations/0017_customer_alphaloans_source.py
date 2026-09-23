from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('accounts', '0016_lender_user_customer')]

    operations = [
        migrations.AlterField(
            model_name='customer',
            name='source',
            field=models.CharField(
                choices=[('organic', 'Organic'), ('arrive', 'Arrive'), ('alphaloans', 'Alphaloans')],
                db_index=True, default='organic', max_length=20,
            ),
        ),
    ]
