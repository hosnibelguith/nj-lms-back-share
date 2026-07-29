# Generated for separating AI decision from loan workflow status.

from django.db import migrations, models


def migrate_ai_statuses(apps, schema_editor):
    Loan = apps.get_model('loans', 'Loan')

    mappings = {
        'ai_approved': 'approved',
        'ai_declined': 'declined',
        'review_required': 'review_required',
    }

    for old_status, ai_decision in mappings.items():
        loans = Loan.objects.filter(status=old_status)
        updates = {
            'ai_decision': ai_decision,
            'status': 'pending',
            'is_active': True,
        }
        if old_status == 'ai_declined':
            updates['declined_at'] = None
            updates['decline_reason'] = None
        loans.update(**updates)

    Loan.objects.filter(status='human_approved').update(status='pending_funding')
    Loan.objects.filter(status='pending', customer__banking_verified=False).update(
        status='ibv_pending'
    )
    Loan.objects.filter(
        status='pending',
        customer__banking_verified=True,
        customer__contract_completed=False,
    ).update(status='pending_signature')


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0007_fundingmethodrecommendation'),
    ]

    operations = [
        migrations.AddField(
            model_name='loan',
            name='ai_decision',
            field=models.CharField(
                blank=True,
                choices=[
                    ('approved', 'Approved'),
                    ('declined', 'Declined'),
                    ('review_required', 'Review Required'),
                ],
                db_index=True,
                max_length=20,
                null=True,
            ),
        ),
        migrations.RunPython(migrate_ai_statuses, noop_reverse),
        migrations.AlterField(
            model_name='loan',
            name='status',
            field=models.CharField(
                choices=[
                    ('ibv_pending', 'IBV Pending'),
                    ('pending', 'Pending Human Decision'),
                    ('pending_signature', 'Pending Signature'),
                    ('human_declined', 'Human Declined'),
                    ('pending_funding', 'Pending Funding'),
                    ('active', 'Active'),
                    ('paid_off', 'Paid Off'),
                    ('defaulted', 'In Collections'),
                ],
                default='pending',
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name='loanstateevent',
            name='event_type',
            field=models.CharField(
                choices=[
                    ('pending_signature', 'Pending Signature'),
                    ('contract_signed', 'Contract Signed'),
                    ('ai_decision', 'AI Decision'),
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
