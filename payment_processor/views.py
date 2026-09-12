import base64
import csv
import hashlib
import hmac
import io
import json
from datetime import timedelta

from django.http import StreamingHttpResponse
from django.db import transaction
from django.utils.decorators import method_decorator
from django.utils import timezone
from rest_framework import permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .access import OwnerOnly, configuration, is_owner, writes_enabled
from .client import ZumClient, ProviderError, sanitize, unwrap, integration_setting, bank_payload
from .models import Operation, SyncRun, WebhookInbox, WorkerLease
from .operations import prepare, execute, ACTIONS
from .reports import Filters, filtered_rows, report_data
from .sync import RESOURCES, cached, latest_run, queue_sync, transaction_detail, wallets


def run_info(run):
    return None if not run else {'id': str(run.pk), 'resource': run.resource, 'status': run.status,
        'received': run.fetched_count, 'expected': run.expected_count, 'started_at': run.started_at,
        'completed_at': run.completed_at, 'heartbeat_at': run.heartbeat_at, 'retry_at': run.retry_at, 'error': run.error}


def filters_for(request):
    serializer = Filters(data=request.query_params)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


class ProcessorView(APIView):
    permission_classes = [OwnerOnly]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store, private'
        return response

    def handle_exception(self, error):
        if isinstance(error, ValidationError):
            detail = error.detail
            if isinstance(detail, dict):
                message = '; '.join(f'{key}: ' + ', '.join(map(str, value if isinstance(value, list) else [value])) for key, value in detail.items())
            else:
                message = ', '.join(map(str, detail)) if isinstance(detail, list) else str(detail)
            return Response({'error': message}, status=400)
        if isinstance(error, ProviderError):
            response = Response({'error': str(error), 'retry_after': error.retry_after}, status=502 if error.status == 401 else error.status)
            if error.retry_after:
                response['Retry-After'] = str(error.retry_after)
            return response
        return super().handle_exception(error)


class AvailabilityView(ProcessorView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not is_owner(request.user):
            return Response({'available': False})
        config = configuration()
        return Response({'available': bool(config and config.enabled), 'writes_enabled': writes_enabled(),
                         'owner': True, 'admin_path': '/admin/payment_processor/pluginsettings/'})


class StatusView(ProcessorView):
    def get(self, request):
        client = ZumClient()
        lease = WorkerLease.objects.using('default').filter(scope=client.scope).first()
        runs = list(SyncRun.objects.using('default').filter(scope=client.scope, resource__in=RESOURCES).order_by('-started_at')[:30])
        last = {resource: run_info(latest_run(client.scope, resource)) for resource in RESOURCES}
        return Response({'writes_enabled': writes_enabled(), 'worker_active': bool(lease and lease.expires_at > timezone.now()),
                         'snapshots': last, 'runs': [run_info(run) for run in runs],
                         'reconcile_minutes': configuration().reconcile_minutes,
                         'webhook_pending': WebhookInbox.objects.using('default').filter(scope=client.scope, processed_at__isnull=True).count()})


class SyncView(ProcessorView):
    def post(self, request):
        resources = request.data.get('resources', ['transactions', 'users', 'funding_sources', 'ledger'])
        if not isinstance(resources, list) or not resources or any(resource not in RESOURCES for resource in resources):
            raise ValidationError('Select supported resources to synchronize.')
        runs = queue_sync(ZumClient(), resources)
        return Response({'runs': [run_info(run) for run in runs]}, status=202)


class WalletView(ProcessorView):
    def get(self, request):
        data, stamp = wallets(ZumClient())
        return Response({'results': data, 'synced_at': stamp})


class RecordsView(ProcessorView):
    def get(self, request, resource):
        if resource not in RESOURCES:
            raise ValidationError('Unknown payment processor section.')
        client = ZumClient()
        filters = filters_for(request)
        run = latest_run(client.scope, resource)
        if not run:
            return Response({'available': False, 'snapshot': None, 'results': [], 'count': None,
                             'message': 'Start synchronization to read all ZUM records. Reports appear only after every page is received.'})
        rows = filtered_rows(run, filters).order_by('-created_at', 'provider_id')
        start = (filters['page'] - 1) * filters['page_size']
        return Response({'available': True, 'snapshot': run_info(run), 'count': rows.count(), 'page': filters['page'],
                         'page_size': filters['page_size'], 'results': [bank_payload(row.payload) for row in rows[start:start + filters['page_size']]]})


class DetailView(ProcessorView):
    def get(self, request, resource, provider_id):
        client = ZumClient()
        provider_id = str(provider_id)
        if resource == 'transactions':
            data, stamp = transaction_detail(client, provider_id)
        else:
            paths = {'users': '/user/', 'funding_sources': '/fundingsource/'}
            if resource not in paths:
                raise ValidationError('Unknown detail type.')
            data, stamp = cached(client, resource, provider_id, lambda: unwrap(client.request('GET', paths[resource] + provider_id)), ttl=30)
        return Response({'data': bank_payload(data), 'synced_at': stamp})


class ReportsView(ProcessorView):
    def get(self, request):
        resource = 'transactions'
        client = ZumClient()
        filters = filters_for(request)
        run = latest_run(client.scope, resource)
        if not run:
            return Response({'available': False, 'snapshot': None, 'message': 'No complete transaction snapshot yet. Start synchronization.'})
        qs = filtered_rows(run, filters)
        return Response({'available': True, 'snapshot': run_info(run), 'timezone': filters['timezone'], 'scope_label': 'Bank and wallet transactions',
                         'date_basis': 'CreatedAt', 'stale': run.completed_at < timezone.now() - timedelta(minutes=configuration().reconcile_minutes * 2),
                         **report_data(qs, filters)})


def csv_cell(value):
    value = '' if value is None else str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else value


class ExportView(ProcessorView):
    def get(self, request, resource):
        if resource not in RESOURCES:
            raise ValidationError('Unknown export.')
        run = latest_run(ZumClient().scope, resource)
        if not run:
            raise ValidationError('Finish synchronization before exporting.')
        rows = filtered_rows(run, filters_for(request)).order_by('created_at', 'provider_id')
        fields = {'transactions': ['Id', 'CreatedAt', 'UserName', 'ZumRailsType', 'TransactionMethod', 'TransactionStatus', 'Currency', 'Amount', 'FailedTransactionEvent', 'ClientTransactionId', 'Memo', 'Comment'],
                  'ledger': ['Id', 'CreatedAt', 'Description', 'Debit', 'Credit', 'Balance', 'Type'],
                  'users': ['Id', 'Name', 'FirstName', 'LastName', 'Email', 'PhoneNumber', 'CreatedAt', 'ClientUserId'],
                  'funding_sources': ['Id', 'Name', 'Number', 'FundingSourceStatus', 'FundingSourceType', 'BillingAccount']}[resource]
        def stream():
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(fields)
            yield '\ufeff' + buffer.getvalue()
            for row in rows.iterator(chunk_size=500):
                buffer.seek(0)
                buffer.truncate(0)
                data = {**row.payload, 'UserName': row.user_name}
                writer.writerow([csv_cell(data.get(field)) for field in fields])
                yield buffer.getvalue()
        response = StreamingHttpResponse(stream(), content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="zum-{resource}-{run.pk}.csv"'
        return response


def operation_info(operation):
    return {'id': str(operation.pk), 'action': operation.action, 'summary': operation.summary,
            'status': operation.status, 'result': operation.result, 'created_at': operation.created_at,
            'expires_at': operation.expires_at, 'executed_at': operation.executed_at}


class OperationsView(ProcessorView):
    def get(self, request):
        rows = Operation.objects.using('default').filter(scope=ZumClient().scope, actor_email=request.user.email.casefold(), action__in=ACTIONS).order_by('-created_at')[:100]
        return Response({'results': [operation_info(row) for row in rows]})

    def post(self, request):
        operation = prepare(ZumClient(), request.user, request.data.get('action'), request.data.get('payload'))
        return Response({**operation_info(operation), 'writes_enabled': writes_enabled()}, status=201)


@method_decorator(transaction.non_atomic_requests(using='default'), name='dispatch')
class ExecuteView(ProcessorView):
    def post(self, request, operation_id):
        operation = execute(ZumClient(), request.user, operation_id, request.data.get('payload'), request.data.get('confirmation'))
        return Response(operation_info(operation))


class WebhooksView(ProcessorView):
    def get(self, request):
        rows = WebhookInbox.objects.using('default').filter(scope=ZumClient().scope).order_by('-received_at')[:100]
        return Response({'results': [{'id': row.pk, 'source': row.source, 'received_at': row.received_at,
            'processed_at': row.processed_at, 'error': row.error, 'type': row.payload.get('Type'), 'event': row.payload.get('Event'),
            'transaction_id': (row.payload.get('Data') or {}).get('Id')} for row in rows]})


class WebhookReceiveView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        config = configuration()
        if not config or not config.enabled:
            return Response({'error': 'Payment processor unavailable.'}, status=503)
        raw = request.body
        if len(raw) > 2 * 1024 * 1024:
            return Response(status=413)
        secret = str(integration_setting('ZUMRAILS_WEBHOOK_SECRET', ''))
        supplied = request.headers.get('zumrails-signature', '')
        if not secret:
            return Response(status=503)
        digest = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
        if not (hmac.compare_digest(supplied, digest.hex()) or hmac.compare_digest(supplied, base64.b64encode(digest).decode())):
            return Response(status=403)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get('Data', {}), dict):
                raise ValueError
            scope = ZumClient().scope
        except (ValueError, ProviderError):
            return Response(status=400)
        WebhookInbox.objects.using('default').get_or_create(scope=scope, digest=hashlib.sha256(raw).hexdigest(), defaults={'payload': sanitize(payload)})
        return Response({'received': True})
