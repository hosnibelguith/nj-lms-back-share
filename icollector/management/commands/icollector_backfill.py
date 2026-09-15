import hashlib
import json
from datetime import date, datetime, time, timedelta

from django.core.management.base import BaseCommand, CommandError

from icollector.access import enabled
from icollector.models import RejectDelivery
from icollector.services import TORONTO, enqueue, source_rejects


class Command(BaseCommand):
    help = 'Preview or queue existing Mohawk rejects for one Toronto return date. Never reprocesses payments.'

    def add_arguments(self, parser):
        parser.add_argument('--date', required=True)
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--fingerprint', default='')

    def handle(self, *args, **options):
        try:
            day = date.fromisoformat(options['date'])
        except ValueError:
            raise CommandError('Date must be YYYY-MM-DD.') from None
        start = datetime.combine(day, time.min, tzinfo=TORONTO)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=TORONTO)
        ids = list(source_rejects().filter(effective_returned_at__gte=start,
            effective_returned_at__lt=end).order_by('pk').values_list('pk', flat=True))
        fingerprint = hashlib.sha256('\n'.join(str(pk) for pk in ids).encode()).hexdigest()
        result = {'date': day.isoformat(), 'count': len(ids), 'fingerprint': fingerprint,
            'already_queued': RejectDelivery.objects.using('default').filter(pk__in=ids).count(),
            'already_delivered': RejectDelivery.objects.using('default').filter(pk__in=ids, status='delivered').count(),
            'applied': False}
        if options['apply']:
            if not enabled():
                raise CommandError('Enable iCollector in Django admin before queueing rejects.')
            if options['fingerprint'] != fingerprint:
                raise CommandError('Preview fingerprint is required and must still match the source IDs.')
            result['queued'] = sum(enqueue(pk) for pk in ids)
            result['applied'] = True
        self.stdout.write(json.dumps(result))
