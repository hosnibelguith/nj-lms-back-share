import base64
import hashlib
import hmac
import json
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib import admin
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework.exceptions import ValidationError
from accounts.models import User, GlobalSetting
from loans.models import FundedPayment, CollectionPayment, WebhookEvent
from .access import OWNER_EMAIL
from .admin import PluginSettingsAdmin
from .client import ZumClient, ProviderError, sanitize, page_result
from .models import PluginSettings, SyncRun, SyncRow, Operation, WebhookInbox
from .operations import prepare, execute, action_request
from .reports import Filters, filtered_rows, report_data
from .sync import sync_page, latest_run, row_values, wallets

ID = '11111111-1111-4111-8111-111111111111'
WALLET = '22222222-2222-4222-8222-222222222222'


@override_settings(ZUMRAILS_API_BASE_URL='https://api-sandbox.zumrails.com', ZUMRAILS_USERNAME='test-user', ZUMRAILS_PASSWORD='test-password')
class ProcessorTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('Real HTTP prohibited'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.owner = User.objects.create_user(email=OWNER_EMAIL, password='local-test-only', is_staff=True, user_type='staff', is_superuser=True)
        self.other = User.objects.create_user(email='other@example.test', password='local-test-only', is_staff=True, user_type='staff', is_superuser=True)
        PluginSettings.objects.update_or_create(pk=1, defaults={'enabled': True, 'allow_provider_writes': True})
        self.client.force_authenticate(self.owner)
        self.provider = ZumClient()

    def snapshot(self, status='complete', scope=None):
        return SyncRun.objects.create(scope=scope or self.provider.scope, resource='transactions', status=status,
            fetched_count=1, expected_count=1, completed_at=timezone.now() if status == 'complete' else None)

    def record(self, run, id=ID, **updates):
        payload = {'Id': id, 'Amount': '12.34', 'Currency': 'CAD', 'CreatedAt': '2026-09-11T02:00:00Z',
                   'TransactionStatus': 'Completed', 'ZumRailsType': 'AccountsPayable', 'TransactionMethod': 'Eft', **updates}
        return SyncRow.objects.create(run=run, provider_id=id, **row_values(payload, 'transactions'))

    def test_other_superuser_is_denied_all_plugin_data_and_admin(self):
        self.client.force_authenticate(self.other)
        for path in ('status', 'wallets', 'reports', 'records/transactions', 'webhooks', 'operations'):
            self.assertEqual(self.client.get('/api/payment-processor/' + path + '/').status_code, 403)
        self.assertEqual(self.client.post('/api/payment-processor/sync/', {}, format='json').status_code, 403)
        self.assertEqual(self.client.get('/api/payment-processor/availability/').data, {'available': False})
        model_admin = PluginSettingsAdmin(PluginSettings, admin.site)
        request = Mock(user=self.other)
        self.assertFalse(model_admin.has_module_permission(request))
        self.assertFalse(model_admin.has_view_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))

    def test_owner_requires_active_staff_identity(self):
        self.owner.user_type = 'customer'
        self.owner.save()
        self.assertEqual(self.client.get('/api/payment-processor/status/').status_code, 403)

    def test_unplug_denies_direct_routes_and_stops_sync(self):
        PluginSettings.objects.filter(pk=1).update(enabled=False)
        self.assertFalse(self.client.get('/api/payment-processor/availability/').data['available'])
        self.assertEqual(self.client.get('/api/payment-processor/reports/').status_code, 403)
        self.assertEqual(self.client.post('/api/payment-processor/sync/', {}, format='json').status_code, 403)
        self.assertEqual(SyncRun.objects.count(), 0)

    def test_no_snapshot_is_not_reported_as_zero(self):
        result = self.client.get('/api/payment-processor/reports/')
        self.assertFalse(result.data['available'])
        self.assertNotIn('totals', result.data)

    def test_incomplete_snapshot_does_not_replace_complete_report(self):
        old = self.snapshot()
        self.record(old)
        new = self.snapshot(status='running')
        self.record(new, Amount='9999.00')
        result = self.client.get('/api/payment-processor/reports/')
        self.assertEqual(result.data['totals'][0]['amount'], '12.34')
        self.assertEqual(result.data['snapshot']['id'], str(old.pk))
        self.assertEqual(result['Cache-Control'], 'no-store, private')

    def test_reports_are_scoped_to_current_credentials(self):
        self.record(self.snapshot(scope='another-account'), Amount='9999.00')
        self.assertFalse(self.client.get('/api/payment-processor/reports/').data['available'])

    def test_reports_keep_currencies_statuses_and_decimals_separate(self):
        run = self.snapshot()
        self.record(run, Amount='0.10')
        self.record(run, id=str(uuid.uuid4()), Amount='0.20', TransactionStatus='Failed', FailedTransactionEvent='EftFailedInsufficientFunds')
        self.record(run, id=str(uuid.uuid4()), Amount='100', Currency='USD')
        result = self.client.get('/api/payment-processor/reports/')
        cad, usd = result.data['totals']
        self.assertEqual((cad['amount'], usd['amount']), ('0.30', '100.00'))
        self.assertEqual(cad['completed_amount'], '0.10')
        self.assertEqual(cad['failed_count'], 1)
        self.assertEqual(result.data['failures'][0]['label'], 'EftFailedInsufficientFunds')

    def test_report_dates_respect_toronto_boundary(self):
        self.record(self.snapshot())  # 02:00 UTC is still September 10 in Toronto.
        previous = self.client.get('/api/payment-processor/reports/?start=2026-09-10&end=2026-09-10')
        current = self.client.get('/api/payment-processor/reports/?start=2026-09-11&end=2026-09-11')
        self.assertEqual(previous.data['totals'][0]['count'], 1)
        self.assertEqual(current.data['totals'], [])

    def test_invalid_filters_are_rejected(self):
        for suffix in ('page=0', 'timezone=invalid', 'start=2026-09-11&end=2026-09-10', 'page_size=999'):
            self.assertEqual(self.client.get('/api/payment-processor/reports/?' + suffix).status_code, 400)

    def test_sanitize_removes_nested_secrets_and_masks_bank_cards(self):
        result = sanitize({'Customer': {'ApiKeyVisa': 'SECRET', 'Token': 'SECRET'}, 'CardCvv': 'SECRET',
            'BankAccountInformation': {'AccountNumber': '123456789'}, 'CardNumber': '4111111111111111', 'Bytes': 'SECRET'})
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(result['CardNumber'], '****1111')
        self.assertEqual(result['BankAccountInformation']['AccountNumber'], '****6789')

    def test_live_wallet_activity_wrapper_is_normalized(self):
        page = {'CurrentPage': 1, 'TotalCount': 0, 'Items': []}
        self.assertEqual(page_result({'result': {'WalletTransactions': page}}), page)
        self.assertEqual(page_result({'result': [page]}), page)
        with self.assertRaises(ProviderError):
            page_result({'result': {'Items': []}})

    def test_wallet_never_publishes_invalid_balance_as_zero(self):
        for balance in ('NaN', 'Infinity', None, 'bad'):
            with patch.object(self.provider, 'request', return_value={'result': [{'Id': WALLET, 'Balance': balance, 'Currency': 'CAD'}]}):
                with self.assertRaises(ProviderError):
                    wallets(self.provider, force=True)

    def test_pagination_import_counts_all_pages_before_publication(self):
        run = SyncRun.objects.create(scope=self.provider.scope, resource='transactions')
        items = [{'Id': ID, 'Amount': '1.00', 'Currency': 'CAD'}]
        with patch.object(self.provider, 'request', side_effect=[
            {'result': {'CurrentPage': 1, 'TotalCount': 2, 'Items': items}},
            {'result': {'CurrentPage': 2, 'TotalCount': 2, 'Items': [{'Id': WALLET, 'Amount': '2.00', 'Currency': 'CAD'}]}}]):
            sync_page(self.provider, run)
            self.assertIsNone(latest_run(self.provider.scope, 'transactions'))
            sync_page(self.provider, run)
        self.assertEqual(run.status, 'complete')
        self.assertEqual(run.rows.count(), 2)

    def test_duplicate_or_shifted_pages_never_publish(self):
        for second in ({'CurrentPage': 2, 'TotalCount': 2, 'Items': [{'Id': ID, 'Amount': 1, 'Currency': 'CAD'}]},
                       {'CurrentPage': 2, 'TotalCount': 3, 'Items': [{'Id': WALLET, 'Amount': 1, 'Currency': 'CAD'}]}):
            run = SyncRun.objects.create(scope=self.provider.scope, resource='transactions')
            with patch.object(self.provider, 'request', side_effect=[{'result': {'CurrentPage': 1, 'TotalCount': 2, 'Items': [{'Id': ID, 'Amount': 1, 'Currency': 'CAD'}]}}, {'result': second}]):
                sync_page(self.provider, run)
                sync_page(self.provider, run)
            self.assertEqual(run.status, 'failed')
            self.assertIsNone(latest_run(self.provider.scope, 'transactions'))

    def test_rate_limit_keeps_progress_for_retry(self):
        run = SyncRun.objects.create(scope=self.provider.scope, resource='transactions')
        with patch.object(self.provider, 'request', side_effect=ProviderError('rate limited', status=429, retry_after=60)):
            sync_page(self.provider, run)
        self.assertEqual(run.status, 'running')
        self.assertEqual(run.next_page, 1)
        self.assertGreater(run.retry_at, timezone.now())

    def test_card_reports_require_complete_catalog_and_activity(self):
        users = SyncRun.objects.create(scope=self.provider.scope, resource='users', status='complete', completed_at=timezone.now())
        SyncRow.objects.create(run=users, provider_id=ID, payload={'Id': ID, 'Name': 'Example', 'Currency': 'CAD'})
        cards = SyncRun.objects.create(scope=self.provider.scope, resource='cards')
        with patch.object(self.provider, 'request', return_value={'result': [{'Id': WALLET, 'CardReferenceId': 'reference', 'CardStatus': 'Activated'}]}):
            sync_page(self.provider, cards)
        self.assertEqual(cards.status, 'complete')
        activity = SyncRun.objects.create(scope=self.provider.scope, resource='card_activity')
        with patch.object(self.provider, 'request', side_effect=[
            {'result': {'CurrentPage': 1, 'TotalCount': 2, 'Items': [{'Id': ID, 'Debit': '-12.34', 'Credit': 0, 'Status': 'Posted', 'CreatedAt': '2026-09-10T15:00:00Z'}]}},
            {'result': {'CurrentPage': 2, 'TotalCount': 2, 'Items': [{'Id': WALLET, 'Debit': '-2.00', 'Credit': 0, 'Status': 'Declined', 'CreatedAt': '2026-09-10T15:00:00Z'}]}},
        ]):
            sync_page(self.provider, activity)
            self.assertEqual(activity.status, 'running')
            self.assertFalse(self.client.get('/api/payment-processor/card-reports/').data['available'])
            sync_page(self.provider, activity)
        response = self.client.get('/api/payment-processor/card-reports/')
        self.assertEqual(response.data['cards'], {'count': 1, 'active': 1})
        self.assertEqual(response.data['totals'][0]['amount'], '14.34')
        self.assertEqual(response.data['totals'][0]['failed_count'], 1)

    def test_card_read_rejection_does_not_invent_empty_catalog(self):
        users = SyncRun.objects.create(scope=self.provider.scope, resource='users', status='complete', completed_at=timezone.now())
        SyncRow.objects.create(run=users, provider_id=ID, payload={'Id': ID})
        cards = SyncRun.objects.create(scope=self.provider.scope, resource='cards')
        with patch.object(self.provider, 'request', side_effect=ProviderError('ZUM returned HTTP 403.', status=403)):
            sync_page(self.provider, cards)
        self.assertEqual(cards.status, 'failed')
        self.assertIsNone(latest_run(self.provider.scope, 'cards'))

    def test_export_contains_all_matching_rows_and_escapes_formulas(self):
        run = self.snapshot()
        for index in range(30):
            self.record(run, id=str(uuid.uuid4()), Memo='=DANGEROUS()')
        result = self.client.get('/api/payment-processor/export/transactions/?page_size=1')
        body = b''.join(result.streaming_content).decode()
        self.assertEqual(len(body.splitlines()), 31)
        self.assertIn("'=DANGEROUS()", body)

    def test_local_mutation_guard_cannot_be_changed_from_database(self):
        GlobalSetting.objects.create(key='PAYMENT_PROCESSOR_ALLOW_WRITES', value='True')
        with self.assertRaises(ProviderError):
            self.provider.request('POST', '/transaction', {}, operation_id=uuid.uuid4())
        self.assertFalse(FundedPayment.objects.exists())
        self.assertFalse(CollectionPayment.objects.exists())

    def transaction_payload(self):
        return {'ZumRailsType': 'AccountsPayable', 'TransactionMethod': 'Eft', 'Amount': '12.34', 'Memo': 'Test', 'UserId': ID, 'WalletId': WALLET}

    def test_preparing_action_makes_no_provider_call_and_reuses_duplicate(self):
        one = prepare(self.provider, self.owner, 'create_transaction', self.transaction_payload())
        two = prepare(self.provider, self.owner, 'create_transaction', self.transaction_payload())
        self.assertEqual(one.pk, two.pk)
        self.assertEqual(Operation.objects.count(), 1)
        self.assertFalse(FundedPayment.objects.exists())

    @override_settings(PAYMENT_PROCESSOR_ALLOW_WRITES=True, ZUMRAILS_TRANSACTIONS_ENABLED=True)
    def test_execute_is_idempotent_and_never_calls_lms_payments(self):
        raw = self.transaction_payload()
        operation = prepare(self.provider, self.owner, 'create_transaction', raw)
        with patch.object(self.provider, 'request', return_value={'result': {'Id': ID}}) as request:
            execute(self.provider, self.owner, operation.pk, raw, 'CONFIRM')
            execute(self.provider, self.owner, operation.pk, raw, 'CONFIRM')
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[2]['ClientTransactionId'], str(operation.pk))
        self.assertFalse(FundedPayment.objects.exists())
        self.assertFalse(CollectionPayment.objects.exists())

    @override_settings(PAYMENT_PROCESSOR_ALLOW_WRITES=True, ZUMRAILS_TRANSACTIONS_ENABLED=True)
    def test_uncertain_actions_are_not_retried(self):
        raw = self.transaction_payload()
        operation = prepare(self.provider, self.owner, 'create_transaction', raw)
        with patch.object(self.provider, 'request', side_effect=ProviderError('Timed out', unknown=True)) as request:
            with self.assertRaises(ProviderError):
                execute(self.provider, self.owner, operation.pk, raw, 'CONFIRM')
            with self.assertRaises(ValidationError):
                execute(self.provider, self.owner, operation.pk, raw, 'CONFIRM')
        self.assertEqual(request.call_count, 1)
        operation.refresh_from_db()
        self.assertEqual(operation.status, 'unknown')

    @override_settings(PAYMENT_PROCESSOR_ALLOW_WRITES=True, ZUMRAILS_TRANSACTIONS_ENABLED=True)
    def test_changed_payload_and_expired_review_do_not_execute(self):
        raw = self.transaction_payload()
        operation = prepare(self.provider, self.owner, 'create_transaction', raw)
        with self.assertRaises(ValidationError):
            execute(self.provider, self.owner, operation.pk, {**raw, 'Amount': '99'}, 'CONFIRM')
        Operation.objects.filter(pk=operation.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(ValidationError):
            execute(self.provider, self.owner, operation.pk, raw, 'CONFIRM')

    def test_funding_source_and_user_rules(self):
        for changes in ({'Amount': '-1'}, {'Amount': 'NaN'}, {'Amount': '0.001'}, {'FundingSourceId': ID}, {'Memo': 'x' * 16}, {'Unexpected': True}):
            with self.assertRaises(ValidationError):
                action_request('create_transaction', {**self.transaction_payload(), **changes})

    def test_batch_preview_preserves_method_and_duplicate_guard(self):
        content = b'first_name;last_name;amount_in_cents;memo\nTest;User;1234;Test\n'
        raw = {'TransactionType': 'AccountsPayable', 'TransactionMethod': 'Eft', 'WalletId': WALLET,
               'Bytes': base64.b64encode(content).decode(), 'FileName': 'payments.csv'}
        operation = prepare(self.provider, self.owner, 'validate_batch', raw)
        self.assertEqual(operation.summary['rows'], 1)
        self.assertEqual(operation.summary['total_amount'], '12.34')
        self.assertNotIn('Bytes', operation.summary)
        _, _, payload = action_request('process_batch', raw)
        self.assertEqual(payload['TransactionMethod'], 'Eft')
        self.assertTrue(payload['SkipFileAlreadyProcessedInLast24Hours'])

    @override_settings(PAYMENT_PROCESSOR_ALLOW_WRITES=True, ZUMRAILS_TRANSACTIONS_ENABLED=True)
    def test_processing_batch_requires_successful_matching_validation(self):
        raw = {'TransactionType': 'AccountsPayable', 'TransactionMethod': 'Eft', 'WalletId': WALLET,
               'Bytes': base64.b64encode(b'first_name;amount_in_cents\nTest;100\n').decode(), 'FileName': 'payments.csv', 'ValidationId': ID}
        operation = prepare(self.provider, self.owner, 'process_batch', raw)
        with patch.object(self.provider, 'request') as provider_request:
            with self.assertRaises(ValidationError):
                execute(self.provider, self.owner, operation.pk, raw, 'CONFIRM')
        provider_request.assert_not_called()

    @override_settings(PAYMENT_PROCESSOR_ALLOW_WRITES=True, ZUMRAILS_TRANSACTIONS_ENABLED=True)
    def test_card_mutation_checks_user_card_relationship(self):
        raw = {'UserId': ID, 'UserCardId': WALLET}
        operation = prepare(self.provider, self.owner, 'activate_card', raw)
        with patch.object(self.provider, 'request', return_value={'result': []}) as provider_request:
            with self.assertRaises(ProviderError):
                execute(self.provider, self.owner, operation.pk, raw, 'CONFIRM')
        self.assertEqual(provider_request.call_count, 1)
        self.assertEqual(provider_request.call_args.args[0], 'GET')

    @override_settings(ZUMRAILS_WEBHOOK_SECRET='test-signature-secret')
    def test_signed_webhook_is_durable_idempotent_and_does_not_modify_lms(self):
        payload = json.dumps({'Type': 'Transaction', 'Data': {'Id': ID, 'CardCvv': 'SECRET'}}).encode()
        signature = hmac.new(b'test-signature-secret', payload, hashlib.sha256).hexdigest()
        for _ in range(2):
            result = self.client.post('/api/payment-processor/webhook/', payload, content_type='application/json', HTTP_ZUMRAILS_SIGNATURE=signature)
            self.assertEqual(result.status_code, 200)
        self.assertEqual(WebhookInbox.objects.count(), 1)
        self.assertNotIn('SECRET', json.dumps(WebhookInbox.objects.get().payload))
        self.assertFalse(WebhookEvent.objects.exists())
        self.assertFalse(FundedPayment.objects.exists())
        self.assertFalse(CollectionPayment.objects.exists())

    @override_settings(ZUMRAILS_WEBHOOK_SECRET='test-signature-secret')
    def test_invalid_webhook_signature_cannot_persist(self):
        result = self.client.post('/api/payment-processor/webhook/', {'Type': 'Transaction'}, format='json', HTTP_ZUMRAILS_SIGNATURE='wrong')
        self.assertEqual(result.status_code, 403)
        self.assertFalse(WebhookInbox.objects.exists())
