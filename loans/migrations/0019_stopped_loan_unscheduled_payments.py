from django.db import migrations, models


LOAN_STATUS_CHOICES = [
    ("ibv_pending", "IBV Pending"),
    ("pending", "Pending Human Decision"),
    ("pending_signature", "Pending Signature"),
    ("human_declined", "Human Declined"),
    ("expired", "Expired"),
    ("pending_funding", "Pending Funding"),
    ("active", "Active"),
    ("paid_off", "Paid Off"),
    ("defaulted", "In Collections"),
    ("stopped", "Stopped"),
]

EVENT_TYPE_CHOICES = [
    ("pending_signature", "Pending Signature"),
    ("contract_signed", "Contract Signed"),
    ("ai_decision", "AI Decision"),
    ("amount_updated", "Amount Updated"),
    ("human_approved", "Human Approved"),
    ("human_declined", "Human Declined"),
    ("expired", "Expired"),
    ("funded", "Funded"),
    ("paid_off", "Paid Off"),
    ("defaulted", "Defaulted"),
    ("stopped", "Stopped"),
    ("reactivated", "Reactivated"),
]

PAYMENT_STATUS_CHOICES = [
    ("scheduled", "Scheduled"),
    ("pending", "Processing"),
    ("completed", "Completed"),
    ("failed", "Failed"),
    ("nsf", "NSF"),
    ("cancelled", "Cancelled"),
    ("unscheduled", "Unscheduled"),
]


def reclassify_stopped_loans(apps, schema_editor):
    Loan = apps.get_model("loans", "Loan")
    Payment = apps.get_model("loans", "Payment")
    from loans.collection_policy import should_convert_defaulted_to_stopped

    for loan in Loan.objects.filter(status="defaulted"):
        if not should_convert_defaulted_to_stopped(loan):
            continue
        loan.status = "stopped"
        loan.is_active = False
        loan.save(update_fields=["status", "is_active"])
        Payment.objects.filter(loan_id=loan.id, status="scheduled").update(status="unscheduled")


def noop_reverse(apps, schema_editor):
    return None


class Migration(migrations.Migration):

    dependencies = [
        ("loans", "0018_loan_schedule_frequency"),
    ]

    operations = [
        migrations.AlterField(
            model_name="loan",
            name="status",
            field=models.CharField(
                choices=LOAN_STATUS_CHOICES,
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="loanstateevent",
            name="event_type",
            field=models.CharField(choices=EVENT_TYPE_CHOICES, max_length=30),
        ),
        migrations.AlterField(
            model_name="payment",
            name="status",
            field=models.CharField(
                choices=PAYMENT_STATUS_CHOICES,
                default="scheduled",
                max_length=20,
            ),
        ),
        migrations.RunPython(reclassify_stopped_loans, noop_reverse),
    ]
