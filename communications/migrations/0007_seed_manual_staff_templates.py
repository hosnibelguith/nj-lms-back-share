from django.db import migrations


def _body(name: str) -> str:
    return (
        f"*Version française suivra*\n\n"
        f"Hi {{{{customer_first_name}}}},\n\n"
        f"[Edit this template body for: {name}]\n\n"
        f"Thank you.\n\n"
        f"---\n\n"
        f"Bonjour {{{{customer_first_name}}}},\n\n"
        f"[Modifier le contenu de ce modèle : {name}]\n\n"
        f"Merci."
    )


# Legacy staff Hot Key / Template names used in the LMS send-email composer.
MANUAL_TEMPLATES = [
    ("001", "Acc closed"),
    ("002", "Amount approved lower than requested"),
    ("003", "Approved Loan 'IBV'"),
    ("004", 'Approved Loan "LEADS ONLY"'),
    ("005", "Deferral OFF"),
    ("006", "Deferral OFF -24Hr"),
    ("007", "Deferral OK"),
    ("008", "Duplicate Request"),
    ("009", "EMT CLT not deposited"),
    ("010", "Funds Sent EFT"),
    ("011", "Funds Sent INTERAC"),
    ("012", "IBV Request"),
    ("013", "Loan Denied"),
    ("014", "Missing IBV"),
    ("015", "Missing JOB"),
    ("016", "New Contract Available"),
    ("017", "**Payment E-transfer Instructions"),
    ("018", "Pending Request"),
    ("019", "Refer to Portal"),
    ("020", "Refinance accepted"),
]


def seed_manual_templates(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communications", "CommunicationTemplate")
    for hot_key, name in MANUAL_TEMPLATES:
        existing = CommunicationTemplate.objects.filter(name=name, type="email").first()
        if existing:
            updates = []
            if not existing.hot_key:
                existing.hot_key = hot_key
                updates.append("hot_key")
            if not existing.subject:
                existing.subject = name
                updates.append("subject")
            if updates:
                existing.save(update_fields=updates)
            continue
        CommunicationTemplate.objects.create(
            name=name,
            type="email",
            trigger="manual",
            hot_key=hot_key,
            subject=name,
            content=_body(name),
            is_active=True,
        )


def remove_manual_templates(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communications", "CommunicationTemplate")
    names = [name for _, name in MANUAL_TEMPLATES]
    CommunicationTemplate.objects.filter(name__in=names, trigger="manual").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communications", "0006_communicationtemplate_hot_key"),
    ]

    operations = [
        migrations.RunPython(seed_manual_templates, remove_manual_templates),
    ]
