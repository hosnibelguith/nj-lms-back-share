from django.db import migrations


DENY_TEMPLATE = """*Version française suivra*

Hi,

Following your request, we have completed a thorough re-evaluation of your file. After careful consideration, we regret to inform you that the decision to decline your loan application remains unchanged.

We invite you to apply within a minimum of 1 month for a re-evaluation.

Thank you for understanding

------------

Bonjour,

Suite à votre demande, nous avons procédé à une réévaluation complète de votre dossier. Après un examen attentif, nous regrettons de vous informer que la décision de refuser votre demande de prêt demeure inchangée.

Nous vous invitons à soumettre une nouvelle demande dans un délai minimum de 1 mois pour une nouvelle évaluation.

Merci de votre compréhension."""


FUNDED_TEMPLATE = """*Version française suivra*

Hi,

We are pleased to inform you that your loan request has been approved.

Your funds are now being processed. Based on the funding method assigned to your file, the funds will either be deposited directly into your chequing account or sent to you shortly by Interac e-Transfer.

Thank you.

---

Bonjour,

Nous avons le plaisir de vous informer que votre demande de prêt a été approuvée.

Vos fonds sont maintenant en cours de traitement. Selon la méthode de financement assignée à votre dossier, les fonds seront soit déposés directement dans votre compte chèque, soit envoyés sous peu par virement Interac.

Merci."""


REQUEST_RECEIVED_TEMPLATE = """*Version française suivra*

Hi,

We have received your request, and your contract has already been signed.

One of our agents will now review and evaluate your file. If your request is approved, the funds will be sent to you, and you will automatically receive an email confirmation.

We will contact you if any additional information is required.

Thank you for your patience.

---

Bonjour,

Nous avons reçu votre demande, et votre contrat a déjà été signé.

Un de nos agents va maintenant examiner et évaluer votre dossier. Si votre demande est approuvée, les fonds vous seront envoyés et vous recevrez automatiquement une confirmation par courriel.

Nous communiquerons avec vous si des informations supplémentaires sont requises.

Merci de votre patience."""


TEMPLATES = [
    {
        "name": "Deny Template",
        "type": "email",
        "trigger": "manual",
        "subject": "Loan application update",
        "content": DENY_TEMPLATE,
    },
    {
        "name": "Fund/Approve Template",
        "type": "email",
        "trigger": "loan_funded",
        "subject": "Your loan request has been approved",
        "content": FUNDED_TEMPLATE,
    },
    {
        "name": "We Have Received Your Request Template",
        "type": "email",
        "trigger": "manual",
        "subject": "We have received your request",
        "content": REQUEST_RECEIVED_TEMPLATE,
    },
]


def seed_templates(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communications", "CommunicationTemplate")
    for template in TEMPLATES:
        CommunicationTemplate.objects.update_or_create(
            name=template["name"],
            defaults={
                **template,
                "html_content": None,
                "is_active": True,
            },
        )


def remove_templates(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communications", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(
        name__in=[template["name"] for template in TEMPLATES]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communications", "0003_communication_is_unknown_sender_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_templates, remove_templates),
    ]
