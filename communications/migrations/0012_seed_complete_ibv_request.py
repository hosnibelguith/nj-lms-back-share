from django.db import migrations


IBV_REQUEST_SUBJECT = "Compléter votre demande IBV"

IBV_REQUEST_TEXT = """Bonjour {{customer_first_name}},

Cliquez sur le bouton dans le courriel HTML, ou ouvrez ce lien pour compléter votre demande IBV :
{{ibv_url}}

Hello {{customer_first_name}},

Use the button in the HTML email, or open this link to complete your IBV request:
{{ibv_url}}

Cordialement / Kind regards,
MohawkLoans

Heures d'ouverture : Lundi - Vendredi de 10:00 à 17:00 (EST)
Opening hours: Monday - Friday, 10am to 5pm EST
"""

IBV_REQUEST_HTML = """<!DOCTYPE html>
<html lang="fr">
  <head>
    <meta charset="UTF-8" />
    <title>Compléter votre demande IBV</title>
    <style>
      body { margin: 0; padding: 0; font-family: Arial, sans-serif; }
      table { border-collapse: collapse; }
      a.button { color: #ffffff !important; text-decoration: none; }
      @media (prefers-color-scheme: dark) {
        a.button { color: #ffffff !important; background-color: #1a73e8 !important; }
      }
    </style>
  </head>
  <body>
    <table width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width: 600px; margin: 0 auto;">
      <tr>
        <td style="padding: 20px;">
          <p>Bonjour {{customer_first_name}},</p>
          <p>Cliquez sur le bouton ci-dessous pour compléter votre demande IBV.</p>
          <p>Hello {{customer_first_name}},</p>
          <p>Use the button below to complete your IBV request.</p>
          <table border="0" cellspacing="0" cellpadding="0" style="margin: 24px 0;">
            <tr>
              <td align="center" style="border-radius: 6px;" bgcolor="#1a73e8">
                <a href="{{ibv_url}}" target="_blank" class="button" style="font-size: 16px; line-height: 24px; font-family: Arial, sans-serif; font-weight: bold; text-decoration: none; display: inline-block; padding: 12px 28px; background-color: #1a73e8; color: #ffffff !important; border-radius: 6px; border: 1px solid #1a73e8;">
                  Fill IBV Request
                </a>
              </td>
            </tr>
          </table>
          <p>
            Cordialement / Kind regards,<br />
            MohawkLoans
          </p>
          <hr style="margin: 24px 0" />
          <p>
            Heures d'ouverture : Lundi - Vendredi de 10:00 &agrave; 17:00 (EST)<br />
            Opening hours: Monday - Friday, 10am to 5pm EST
          </p>
        </td>
      </tr>
    </table>
  </body>
</html>
"""


def seed_complete_ibv_request(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communications", "CommunicationTemplate")
    defaults = {
        "type": "email",
        "trigger": "manual",
        "subject": IBV_REQUEST_SUBJECT,
        "content": IBV_REQUEST_TEXT,
        "html_content": IBV_REQUEST_HTML,
        "is_active": True,
    }
    template = CommunicationTemplate.objects.filter(name="Complete IBV Request").order_by(
        "created_at"
    ).first()
    if template:
        for key, value in defaults.items():
            setattr(template, key, value)
        template.save()
        return
    CommunicationTemplate.objects.create(name="Complete IBV Request", **defaults)


def unseed_complete_ibv_request(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communications", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(name="Complete IBV Request").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communications", "0011_update_early_renewal_offer_amounts"),
    ]

    operations = [
        migrations.RunPython(seed_complete_ibv_request, unseed_complete_ibv_request),
    ]
