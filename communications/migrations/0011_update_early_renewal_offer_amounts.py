from django.db import migrations


EARLY_RENEWAL_TEMPLATE = """*Version française suivra*

Hi {{customer_first_name}},

You qualify for an early renewal.

New loan amount: ${{new_loan_amount}}
Remaining balance on your current loan: ${{remaining_balance}}
Amount deducted to close your current loan: ${{amount_deducted}}
Amount you will receive: ${{net_to_client}}

Log in to start your renewal:
{{portal_url}}

Thank you.

---

Bonjour {{customer_first_name}},

Vous êtes admissible à un renouvellement anticipé.

Montant du nouveau prêt : {{new_loan_amount}} $
Solde restant sur votre prêt actuel : {{remaining_balance}} $
Montant déduit pour fermer votre prêt actuel : {{amount_deducted}} $
Montant que vous recevrez : {{net_to_client}} $

Connectez-vous pour commencer votre renouvellement :
{{portal_url}}

Merci."""


def update_early_renewal_offer(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communications", "CommunicationTemplate")
    template = CommunicationTemplate.objects.filter(name="Early Renewal Offer").order_by(
        "created_at"
    ).first()
    if template is None:
        return
    template.subject = "You qualify for an early renewal"
    template.content = EARLY_RENEWAL_TEMPLATE
    template.save(update_fields=["subject", "content", "updated_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("communications", "0010_seed_early_renewal_offer"),
    ]

    operations = [
        migrations.RunPython(update_early_renewal_offer, migrations.RunPython.noop),
    ]
