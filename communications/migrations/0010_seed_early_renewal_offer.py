from django.db import migrations


EARLY_RENEWAL_TEMPLATE = """*Version française suivra*

Hi {{customer_first_name}},

You qualify for an early renewal.

Remaining balance on your current loan: ${{old_balance}}
New loan amount: ${{new_loan_amount}}
Amount sent to you after the old balance is deducted: ${{net_to_client}}

Log in to start your renewal:
{{portal_url}}

Thank you.

---

Bonjour {{customer_first_name}},

Vous êtes admissible à un renouvellement anticipé.

Solde restant sur votre prêt actuel : {{old_balance}} $
Montant du nouveau prêt : {{new_loan_amount}} $
Montant qui vous sera envoyé après déduction de l’ancien solde : {{net_to_client}} $

Connectez-vous pour commencer votre renouvellement :
{{portal_url}}

Merci."""


def seed_early_renewal_offer(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communications", "CommunicationTemplate")
    defaults = {
        "type": "email",
        "trigger": "manual",
        "subject": "You qualify for an early renewal",
        "content": EARLY_RENEWAL_TEMPLATE,
        "html_content": None,
        "is_active": True,
    }
    template = CommunicationTemplate.objects.filter(name="Early Renewal Offer").order_by(
        "created_at"
    ).first()
    if template:
        for key, value in defaults.items():
            setattr(template, key, value)
        template.save()
        return
    CommunicationTemplate.objects.create(name="Early Renewal Offer", **defaults)


def unseed_early_renewal_offer(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communications", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(name="Early Renewal Offer").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communications", "0009_seed_application_expired_workflow"),
    ]

    operations = [
        migrations.RunPython(seed_early_renewal_offer, unseed_early_renewal_offer),
    ]
