from django.db import migrations


def create_disabled_switch(apps, schema_editor):
    apps.get_model('icollector', 'PluginSettings').objects.using(
        schema_editor.connection.alias).get_or_create(pk=1, defaults={'enabled': False})


class Migration(migrations.Migration):
    dependencies = [('icollector', '0001_initial')]
    operations = [migrations.RunPython(create_disabled_switch, migrations.RunPython.noop)]
