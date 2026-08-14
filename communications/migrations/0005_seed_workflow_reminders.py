from django.db import migrations


IBV_TEMPLATE = """*Version française suivra*

Hi {{customer_first_name}},

This is a reminder to complete your banking verification so we can continue reviewing your loan request.

Please complete IBV here:
{{portal_url}}

Thank you.

---

Bonjour {{customer_first_name}},

Ceci est un rappel pour compléter votre vérification bancaire afin que nous puissions poursuivre l'examen de votre demande de prêt.

Veuillez compléter l'IBV ici :
{{portal_url}}

Merci."""


SIGNATURE_TEMPLATE = """*Version française suivra*

Hi {{customer_first_name}},

Your loan has been approved, but we still need your signed contract before funding can be completed.

Please sign your contract here:
{{portal_url}}

Thank you.

---

Bonjour {{customer_first_name}},

Votre prêt a été approuvé, mais nous avons encore besoin de votre contrat signé avant que le financement puisse être complété.

Veuillez signer votre contrat ici :
{{portal_url}}

Merci."""


SETTINGS = [
    {
        "key": "LOAN_WORKFLOW_REMINDERS_ENABLED",
        "value": "True",
        "description": "Enable daily reminders for pending IBV and approved loans waiting for contract signature.",
    },
    {
        "key": "LOAN_WORKFLOW_REMINDER_MAX_DAYS",
        "value": "3",
        "description": "Maximum number of daily workflow reminders to send per loan and reminder type.",
    },
    {
        "key": "LOAN_WORKFLOW_REMINDER_TIMEZONE",
        "value": "America/New_York",
        "description": "Timezone used when checking whether a workflow reminder was already sent today.",
    },
]


def seed_workflow_reminders(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communications", "CommunicationTemplate")
    GlobalSetting = apps.get_model("accounts", "GlobalSetting")
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    CommunicationTemplate.objects.update_or_create(
        name="IBV Reminder Template",
        defaults={
            "type": "email",
            "trigger": "manual",
            "subject": "Reminder: complete your banking verification",
            "content": IBV_TEMPLATE,
            "html_content": None,
            "is_active": True,
        },
    )
    CommunicationTemplate.objects.update_or_create(
        name="Contract Signature Reminder Template",
        defaults={
            "type": "email",
            "trigger": "manual",
            "subject": "Reminder: please sign your loan contract",
            "content": SIGNATURE_TEMPLATE,
            "html_content": None,
            "is_active": True,
        },
    )

    for setting in SETTINGS:
        GlobalSetting.objects.update_or_create(
            key=setting["key"],
            defaults={
                "value": setting["value"],
                "description": setting["description"],
                "is_secret": False,
            },
        )

    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="30",
        hour="8",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="America/New_York",
    )
    PeriodicTask.objects.update_or_create(
        name="Send loan workflow reminders",
        defaults={
            "task": "communications.tasks.send_loan_workflow_reminders",
            "crontab": crontab,
            "enabled": True,
            "description": "Daily reminders for pending IBV and approved loans waiting for contract signature.",
        },
    )


def remove_workflow_reminders(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communications", "CommunicationTemplate")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    CommunicationTemplate.objects.filter(
        name__in=["IBV Reminder Template", "Contract Signature Reminder Template"]
    ).delete()
    PeriodicTask.objects.filter(name="Send loan workflow reminders").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0011_arrive_integration_fields"),
        ("communications", "0004_seed_loan_status_templates"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(seed_workflow_reminders, remove_workflow_reminders),
    ]
