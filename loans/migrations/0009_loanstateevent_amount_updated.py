# Generated for approved amount update audit events.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0008_separate_ai_decision'),
    ]

    operations = [
        migrations.AlterField(
            model_name='loanstateevent',
            name='event_type',
            field=models.CharField(
                choices=[
                    ('pending_signature', 'Pending Signature'),
                    ('contract_signed', 'Contract Signed'),
                    ('ai_decision', 'AI Decision'),
                    ('amount_updated', 'Amount Updated'),
                    ('human_approved', 'Human Approved'),
                    ('human_declined', 'Human Declined'),
                    ('funded', 'Funded'),
                    ('paid_off', 'Paid Off'),
                    ('defaulted', 'Defaulted'),
                    ('reactivated', 'Reactivated'),
                ],
                max_length=30,
            ),
        ),
    ]
