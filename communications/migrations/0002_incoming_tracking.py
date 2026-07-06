# Generated for Incoming Comms and engagement tracking.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('communications', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='communication',
            name='type',
            field=models.CharField(
                choices=[
                    ('email', 'Email'),
                    ('sms', 'SMS'),
                    ('notification', 'Notification'),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='communication',
            name='incoming_status',
            field=models.CharField(
                choices=[
                    ('new', 'New'),
                    ('unanswered', 'Unanswered'),
                    ('read', 'Read'),
                ],
                db_index=True,
                default='new',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='communication',
            name='is_answered',
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name='communication',
            name='opened_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='communication',
            name='opened_by',
            field=models.EmailField(blank=True, max_length=254, null=True),
        ),
    ]
