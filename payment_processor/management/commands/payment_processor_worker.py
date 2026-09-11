import json
import time
import uuid
from datetime import timedelta
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction, close_old_connections
from django.utils import timezone
from payment_processor.access import configuration
from payment_processor.client import ZumClient, ProviderError
from payment_processor.models import SyncRun, WorkerLease
from payment_processor.sync import run_cycle, queue_sync, CORE_RESOURCES


class Command(BaseCommand):
    help = 'Run only the payment processor read/sync worker. Does not start LMS Celery tasks or create payments.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Synchronize all core resources once, then exit.')
        parser.add_argument('--tenant', default='default')

    def handle(self, *args, **options):
        from config.tenant_context import use_tenant_database
        if options['tenant'] not in settings.DATABASES:
            raise CommandError('Unknown tenant database.')
        with use_tenant_database(options['tenant']):
            self.work(options['once'])

    def work(self, once):
        holder = uuid.uuid4()
        initialized = False
        while True:
            config = configuration()
            if not config or not config.enabled:
                if once:
                    raise CommandError('Payment processor is unplugged in Django admin.')
                time.sleep(2)
                continue
            client = ZumClient()
            WorkerLease.objects.using('default').get_or_create(scope=client.scope)
            with transaction.atomic(using='default'):
                lease = WorkerLease.objects.using('default').select_for_update().get(scope=client.scope)
                if lease.expires_at > timezone.now() and lease.holder != holder:
                    if once:
                        raise CommandError('A payment processor worker is already active.')
                    acquired = False
                else:
                    acquired = True
                    lease.holder = holder
                    lease.expires_at = timezone.now() + timedelta(minutes=3)
                    lease.save(using='default')
            if not acquired:
                time.sleep(2)
                continue
            if not initialized:
                if once:
                    queue_sync(client)
                initialized = True
            run = None
            try:
                run = run_cycle(client)
                if run:
                    self.stdout.write(json.dumps({'resource': run.resource, 'status': run.status,
                                                  'received': run.fetched_count, 'expected': run.expected_count,
                                                  'error': run.error or None}))
                if once and not SyncRun.objects.using('default').filter(scope=client.scope, status__in=['queued', 'running']).exists():
                    WorkerLease.objects.using('default').filter(scope=client.scope, holder=holder).update(expires_at=timezone.now())
                    failed = any(SyncRun.objects.using('default').filter(scope=client.scope, resource=resource).latest('started_at').status != 'complete' for resource in CORE_RESOURCES)
                    if failed:
                        raise CommandError('One or more resources failed. See synchronization status; incomplete reports were not published.')
                    return
            except ProviderError as error:
                self.stderr.write(str(error))
                if once:
                    raise CommandError(str(error)) from None
            close_old_connections()
            time.sleep(1 if run else 2)
