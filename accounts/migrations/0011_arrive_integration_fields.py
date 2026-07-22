import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0010_alter_customer_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='customer',
            name='arrive_application_id',
            field=models.CharField(blank=True, db_index=True, max_length=100, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='customer',
            name='arrive_event_id',
            field=models.CharField(blank=True, db_index=True, max_length=100, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='customer',
            name='arrive_zum_user_card_id',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='customer',
            name='arrive_zum_user_id',
            field=models.CharField(blank=True, db_index=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='customer',
            name='source',
            field=models.CharField(
                choices=[('organic', 'Organic'), ('arrive', 'Arrive')],
                db_index=True,
                default='organic',
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name='ArriveHandoffToken',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('token', models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True)),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('consumed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                (
                    'customer',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='arrive_handoff_tokens',
                        to='accounts.customer',
                    ),
                ),
            ],
            options={
                'db_table': 'accounts_arrive_handoff_token',
                'ordering': ['-created_at'],
            },
        ),
    ]
