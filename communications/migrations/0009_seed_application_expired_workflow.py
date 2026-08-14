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


EXPIRED_TEMPLATE = """*Version française suivra*

Hi {{customer_first_name}},

Your loan application has expired because banking verification was not completed in time.

You can log in anytime and start a new application here:
{{portal_url}}

Thank you.

---

Bonjour {{customer_first_name}},

Votre demande de prêt a expiré parce que la vérification bancaire n'a pas été complétée à temps.

Vous pouvez vous connecter à tout moment et commencer une nouvelle demande ici :
{{portal_url}}

Merci."""


def seed_application_expired_workflow(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communications", "CommunicationTemplate")
    GlobalSetting = apps.get_model("accounts", "GlobalSetting")

    def upsert_template(name, defaults):
        template = CommunicationTemplate.objects.filter(name=name).order_by("created_at").first()
        if template:
            for key, value in defaults.items():
                setattr(template, key, value)
            template.save()
            return template
        return CommunicationTemplate.objects.create(name=name, **defaults)

    missing_ibv = CommunicationTemplate.objects.filter(name="MISSING IBV Reminder").first()
    exact_ibv = CommunicationTemplate.objects.filter(name="IBV Reminder Template").first()
    if missing_ibv and not exact_ibv:
        missing_ibv.name = "IBV Reminder Template"
        missing_ibv.type = "email"
        missing_ibv.subject = "Reminder: complete your banking verification"
        missing_ibv.content = missing_ibv.content or IBV_TEMPLATE
        missing_ibv.is_active = True
        missing_ibv.save()
    else:
        upsert_template(
            "IBV Reminder Template",
            {
                "type": "email",
                "trigger": "manual",
                "subject": "Reminder: complete your banking verification",
                "content": IBV_TEMPLATE,
                "html_content": None,
                "is_active": True,
            },
        )

    upsert_template(
        "Application Expired Template",
        {
            "type": "email",
            "trigger": "manual",
            "subject": "Your loan application has expired",
            "content": EXPIRED_TEMPLATE,
            "html_content": None,
            "is_active": True,
        },
    )

    GlobalSetting.objects.update_or_create(
        key="LOAN_WORKFLOW_REMINDER_MAX_DAYS",
        defaults={
            "value": "3",
            "description": "Maximum number of daily workflow reminders to send per loan and reminder type.",
            "is_secret": False,
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0012_customer_sms_opt_out_reason_customer_sms_opted_out_and_more"),
        ("communications", "0008_communication_answered_at"),
    ]

    operations = [
        migrations.RunPython(seed_application_expired_workflow, migrations.RunPython.noop),
    ]
