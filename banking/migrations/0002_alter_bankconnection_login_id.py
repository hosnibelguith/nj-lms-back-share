from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('banking', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='bankconnection',
            name='login_id',
            field=models.CharField(db_index=True, help_text='Flinks Login ID', max_length=255),
        ),
    ]
