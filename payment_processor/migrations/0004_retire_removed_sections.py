from django.db import migrations


def retire_jobs(apps, schema_editor):
    runs = apps.get_model('payment_processor', 'SyncRun')
    runs.objects.using(schema_editor.connection.alias).filter(
        resource__in=['cards', 'card_activity', 'invoices', 'subscriptions'],
        status__in=['queued', 'running'],
    ).update(status='cancelled', error='This section has been removed.')


class Migration(migrations.Migration):
    dependencies = [('payment_processor', '0003_syncrun_cursor')]
    operations = [migrations.RunPython(retire_jobs, migrations.RunPython.noop)]
