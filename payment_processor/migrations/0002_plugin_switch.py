from django.db import migrations


def create_switch(apps, schema_editor):
    if schema_editor.connection.alias == 'default':
        apps.get_model('payment_processor', 'PluginSettings').objects.using('default').get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [('payment_processor', '0001_initial')]
    operations = [migrations.RunPython(create_switch, migrations.RunPython.noop)]
