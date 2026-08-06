from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("communications", "0005_seed_workflow_reminders"),
    ]

    operations = [
        migrations.AddField(
            model_name="communicationtemplate",
            name="hot_key",
            field=models.CharField(
                blank=True,
                help_text="Optional shortcut code for staff Hot Key selection (e.g. 061).",
                max_length=20,
                null=True,
                unique=True,
            ),
        ),
    ]
