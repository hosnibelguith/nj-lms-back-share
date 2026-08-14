from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("loans", "0014_alter_payment_options_remove_loan_ibv_reset_reason_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="loan",
            name="status",
            field=models.CharField(
                choices=[
                    ("ibv_pending", "IBV Pending"),
                    ("pending", "Pending Human Decision"),
                    ("pending_signature", "Pending Signature"),
                    ("human_declined", "Human Declined"),
                    ("expired", "Expired"),
                    ("pending_funding", "Pending Funding"),
                    ("active", "Active"),
                    ("paid_off", "Paid Off"),
                    ("defaulted", "In Collections"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="loanstateevent",
            name="event_type",
            field=models.CharField(
                choices=[
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
                    ("reactivated", "Reactivated"),
                ],
                max_length=30,
            ),
        ),
    ]
