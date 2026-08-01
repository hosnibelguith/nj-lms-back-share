from django.db import migrations


REPLACEMENTS = (
    (
        "NoJuice powered by MohawkLoans (hereinafter referred to as &quot;MohawkLoans&quot;)",
        "MohawkLoans (hereinafter referred to as &quot;MohawkLoans&quot;)",
    ),
    (
        'NoJuice powered by MohawkLoans (hereinafter referred to as "MohawkLoans")',
        'MohawkLoans (hereinafter referred to as "MohawkLoans")',
    ),
    (
        "NoJuice powered by MohawkLoans (hereinafter &quot;Payee&quot;)",
        "MohawkLoans (hereinafter &quot;Payee&quot;)",
    ),
    (
        'NoJuice powered by MohawkLoans (hereinafter "Payee")',
        'MohawkLoans (hereinafter "Payee")',
    ),
    ("NoJuice powered by MohawkLoans", "MohawkLoans"),
    ("nojuice powered by MohawkLoans", "MohawkLoans"),
    ("No Juice powered by MohawkLoans", "MohawkLoans"),
)


def scrub_nojuice_branding(apps, schema_editor):
    GlobalSetting = apps.get_model("accounts", "GlobalSetting")
    Contract = apps.get_model("contracts", "Contract")

    GlobalSetting.objects.update_or_create(
        key="LENDING_LICENSE_HOLDER",
        defaults={"value": "MohawkLoans"},
    )
    # Also fix if value was literally NoJuice under any casing.
    for setting in GlobalSetting.objects.filter(key="LENDING_LICENSE_HOLDER"):
        if "nojuice" in (setting.value or "").lower().replace(" ", ""):
            setting.value = "MohawkLoans"
            setting.save(update_fields=["value"])

    for contract in Contract.objects.all().iterator():
        text = contract.agreement_text or ""
        updated = text
        for old, new in REPLACEMENTS:
            updated = updated.replace(old, new)
        # Remaining standalone NoJuice labels in lender/payee spots.
        updated = updated.replace(">NoJuice<", ">MohawkLoans<")
        updated = updated.replace(">No Juice<", ">MohawkLoans<")
        if updated != text:
            contract.agreement_text = updated
            contract.save(update_fields=["agreement_text"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("contracts", "0003_alter_contract_agreement_text_and_more"),
        ("accounts", "0011_arrive_integration_fields"),
    ]

    operations = [
        migrations.RunPython(scrub_nojuice_branding, noop_reverse),
    ]
