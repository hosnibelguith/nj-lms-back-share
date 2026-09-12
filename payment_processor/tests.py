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
from rest_framework.test import APITestCase, APITransactionTestCase
from rest_framework.exceptions import ValidationError
from accounts.models import User, GlobalSetting
from loans.models import FundedPayment, CollectionPayment, WebhookEvent
from .access import OWNER_EMAIL, writes_enabled
from .admin import PluginSettingsAdmin
from .client import ZumClient, ProviderError, sanitize, page_result, bank_payload
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


    def test_removed_actions_cannot_be_prepared_even_when_writes_are_enabled(self):
        removed = ['validate_batch', 'process_batch', 'refund_transaction', 'complete_transaction',
                   'activate_card', 'update_card_status', 'update_card_program', 'reissue_card',
                   'pause_subscription', 'resume_subscription']
        for action in removed:
            with self.subTest(action=action):
                response = self.client.post('/api/payment-processor/operations/', {'action': action, 'payload': {}}, format='json')
                self.assertEqual(response.status_code, 400)
        self.assertFalse(Operation.objects.exists())
        for change in [{'ZumRailsType': 'LoadCard'}, {'ZumRailsType': 'StatementPayment'},
                       {'TransactionMethod': 'CreditCard'}, {'TransactionMethod': 'PrepaidCard'}]:
            with self.assertRaises(ValidationError):
                action_request('create_transaction', {**self.transaction_payload(), **change})

    def test_removed_routes_and_sync_jobs_are_unavailable(self):
        for path in ('card-reports/', f'card-activity/{ID}/', 'tib/status/', 'vopay/status/'):
            self.assertEqual(self.client.get('/api/payment-processor/' + path).status_code, 404)
        for resource in ('cards', 'card_activity', 'invoices', 'subscriptions'):
            for prefix in ('records', 'export', 'details'):
                suffix = f'{resource}/{ID}/' if prefix == 'details' else f'{resource}/'
                self.assertEqual(self.client.get(f'/api/payment-processor/{prefix}/' + suffix).status_code, 400)
            self.assertEqual(self.client.post('/api/payment-processor/sync/', {'resources': [resource]}, format='json').status_code, 400)
        run = SyncRun.objects.create(scope=self.provider.scope, resource='cards')
        with patch.object(self.provider, 'request') as request:
            sync_page(self.provider, run)
        request.assert_not_called()
        self.assertEqual(run.status, 'cancelled')
        self.assertNotIn('cards', self.client.get('/api/payment-processor/status/').data['snapshots'])

    def test_bank_report_and_export_exclude_removed_payment_products(self):
        run = self.snapshot()
        self.record(run)
        self.record(run, id=WALLET, TransactionMethod='CreditCard', Amount='100.00')
        self.record(run, id=str(uuid.uuid4()), ZumRailsType='LoadCard', Amount='200.00')
        self.record(run, id=str(uuid.uuid4()), TransactionMethod='VisaDirect', Amount='300.00')
        report = self.client.get('/api/payment-processor/reports/').data
        self.assertEqual(report['totals'][0]['amount'], '12.34')
        self.assertEqual(report['scope_label'], 'Bank and wallet transactions')
        self.assertNotIn('cards', report)
        self.assertEqual(self.client.get('/api/payment-processor/records/transactions/').data['count'], 1)
        export = self.client.get('/api/payment-processor/export/transactions/')
        self.assertEqual(len(b''.join(export.streaming_content).decode().splitlines()), 2)

    def test_existing_user_details_hide_removed_product_fields(self):
        data = {'Id': ID, 'FirstName': 'Example', 'HasPrepaidCardUser': True,
                'TransactionMethodsAvailableDescription': 'Eft, CreditCard, Interac, VisaDirect',
                'BankAccountInformation': {'AccountNumber': '123456789'},
                'Nested': {'Cards': [{'CardNumber': '4111111111111111'}], 'InvoiceId': ID}}
        filtered = bank_payload(data)
        self.assertNotIn('Card', json.dumps(filtered))
        self.assertNotIn('Invoice', json.dumps(filtered))
        self.assertEqual(filtered['TransactionMethodsAvailableDescription'], 'Eft, Interac')
        self.assertEqual(filtered['BankAccountInformation']['AccountNumber'], '****6789')
        with patch.object(ZumClient, 'request', return_value={'result': data}):
            response = self.client.get(f'/api/payment-processor/details/users/{ID}/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('HasPrepaidCardUser', response.data['data'])

    @override_settings(PAYMENT_PROCESSOR_ALLOW_WRITES=True, ZUMRAILS_TRANSACTIONS_ENABLED=True)
    def test_live_flags_enable_owner_actions_but_local_guard_still_wins(self):
        self.assertTrue(writes_enabled())
        self.assertTrue(self.client.get('/api/payment-processor/availability/').data['writes_enabled'])
        with self.settings(ZUMRAILS_TRANSACTIONS_ENABLED=False):
            self.assertFalse(writes_enabled())
            operation = prepare(self.provider, self.owner, 'create_transaction', self.transaction_payload())
            with patch.object(self.provider, 'request') as request:
                with self.assertRaises(ProviderError):
                    execute(self.provider, self.owner, operation.pk, self.transaction_payload(), 'CONFIRM')
            request.assert_not_called()
        PluginSettings.objects.filter(pk=1).update(allow_provider_writes=False)
        self.assertFalse(writes_enabled())

    def test_bank_transaction_flows_validate_for_eft_and_interac(self):
        for flow in ('AccountsPayable', 'AccountsReceivable'):
            for method in ('Eft', 'Interac'):
                verb, path, payload = action_request('create_transaction', {**self.transaction_payload(), 'ZumRailsType': flow, 'TransactionMethod': method})
                self.assertEqual((verb, path, payload['Amount']), ('POST', '/transaction', '12.34'))
        for flow in ('FundZumWallet', 'WithdrawZumWallet'):
            raw = {**self.transaction_payload(), 'ZumRailsType': flow, 'FundingSourceId': ID}
            raw.pop('UserId')
            self.assertEqual(action_request('create_transaction', raw)[2]['WalletId'], WALLET)


    def test_dashboard_separates_ap_ar_wallet_transfers_and_noncompleted_attempts(self):
        run = self.snapshot()
        for flow, status, value in [('AccountsPayable', 'Completed', '100.10'), ('AccountsPayable', 'InProgress', '30.20'),
                                    ('AccountsPayable', 'Failed', '20.30'), ('AccountsPayable', 'Cancelled', '10.40'),
                                    ('AccountsReceivable', 'Completed', '50.05'), ('FundZumWallet', 'Completed', '200.15'),
                                    ('WithdrawZumWallet', 'Completed', '40.10'), ('Other', 'Completed', '9.00')]:
            self.record(run, id=str(uuid.uuid4()), ZumRailsType=flow, TransactionStatus=status, Amount=value,
                        User={'Id': ID, 'Name': 'Example Customer'})
        report = self.client.get('/api/payment-processor/reports/').data
        movement = report['dashboard']['currencies'][0]
        self.assertEqual(movement['ap_amount'], '100.10')
        self.assertEqual(movement['in_amount'], '250.20')
        self.assertEqual(movement['out_amount'], '140.20')
        self.assertEqual(movement['net_amount'], '110.00')
        self.assertEqual((movement['in_count'], movement['out_count']), (2, 2))
        self.assertEqual(movement['unclassified_completed_amount'], '9.00')
        ap = next(row for row in report['flows'] if row['label'] == 'AccountsPayable')
        self.assertEqual((ap['pending_amount'], ap['failed_amount'], ap['cancelled_amount']), ('30.20', '20.30', '10.40'))
        self.assertEqual(ap['success_rate'], '25.00')
        self.assertEqual(ap['completed_average'], '100.10')
        slices = report['dashboard']['ap_statuses']
        self.assertEqual(sum(row['count'] for row in slices), 4)
        self.assertEqual(sum(Decimal(row['amount']) for row in slices), Decimal('161.00'))
        self.assertEqual(report['dashboard']['ap_users'][0]['completed_amount'], '100.10')

    def test_dashboard_periods_dates_currencies_and_negative_net_remain_exact(self):
        run = self.snapshot()
        self.record(run, Amount='0.10')  # September 10 in Toronto.
        self.record(run, id=str(uuid.uuid4()), Amount='0.20', CreatedAt='2026-09-11T16:00:00Z')
        self.record(run, id=str(uuid.uuid4()), Amount='999.00', Currency='USD')
        self.record(run, id=str(uuid.uuid4()), Amount='3.00', CreatedAt=None)
        data = self.client.get('/api/payment-processor/reports/?currency=CAD').data['dashboard']
        self.assertEqual(len(data['currencies']), 1)
        self.assertEqual(data['currencies'][0]['net_amount'], '-3.30')
        self.assertEqual(data['currencies'][0]['undated_count'], 1)
        self.assertEqual([(row['date'], row['ap_amount']) for row in data['trend']], [('2026-09-10', '0.10'), ('2026-09-11', '0.20')])
        week = self.client.get('/api/payment-processor/reports/?currency=CAD&period=week').data['dashboard']['trend']
        self.assertEqual([(row['date'], row['ap_amount']) for row in week], [('2026-09-07', '0.30')])
        month = self.client.get('/api/payment-processor/reports/?currency=CAD&period=month').data['dashboard']['trend']
        self.assertEqual([(row['date'], row['ap_amount']) for row in month], [('2026-09-01', '0.30')])
        filtered = self.client.get('/api/payment-processor/reports/?currency=CAD&start=2026-09-11&end=2026-09-11').data['dashboard']
        self.assertEqual(filtered['currencies'][0]['ap_amount'], '0.20')

    def test_dashboard_drilldown_matches_outcome_direction_and_export(self):
        run = self.snapshot()
        for flow, status in [('AccountsPayable', 'Completed'), ('AccountsPayable', 'Rejected'),
                             ('AccountsPayable', 'PendingCancellation'), ('AccountsReceivable', 'completed'),
                             ('FundZumWallet', 'Completed'), ('WithdrawZumWallet', 'Completed')]:
            self.record(run, id=str(uuid.uuid4()), ZumRailsType=flow, TransactionStatus=status)
        pending = self.client.get('/api/payment-processor/records/transactions/?flow=AccountsPayable&outcome=pending')
        self.assertEqual(pending.data['count'], 1)
        failed = self.client.get('/api/payment-processor/records/transactions/?flow=AccountsPayable&outcome=failed')
        self.assertEqual(failed.data['count'], 1)
        incoming = self.client.get('/api/payment-processor/records/transactions/?direction=in&outcome=completed')
        self.assertEqual(incoming.data['count'], 2)
        report = self.client.get('/api/payment-processor/reports/?direction=in&outcome=completed').data
        self.assertEqual(report['dashboard']['currencies'][0]['in_count'], 2)
        export = self.client.get('/api/payment-processor/export/transactions/?direction=in&outcome=completed')
        self.assertEqual(len(b''.join(export.streaming_content).decode().splitlines()), 3)
        self.assertEqual(self.client.get('/api/payment-processor/reports/?outcome=unknown').status_code, 400)
        empty = self.client.get('/api/payment-processor/reports/?flow=Absent').data
        self.assertEqual(empty['dashboard']['currencies'], [])
        self.assertEqual(empty['dashboard']['ap_statuses'], [])


@override_settings(ZUMRAILS_API_BASE_URL='https://api-sandbox.zumrails.com', ZUMRAILS_USERNAME='test-user',
                   ZUMRAILS_PASSWORD='test-password', PAYMENT_PROCESSOR_ALLOW_WRITES=True, ZUMRAILS_TRANSACTIONS_ENABLED=True)
class ExecutionCommitTests(APITransactionTestCase):
    def test_execution_commits_before_provider_request_and_retains_unknown_result(self):
        from django.db import connection
        owner = User.objects.create_user(email=OWNER_EMAIL, password='test-only', is_staff=True, user_type='staff')
        PluginSettings.objects.update_or_create(pk=1, defaults={'enabled': True, 'allow_provider_writes': True})
        self.client.force_authenticate(owner)
        provider = ZumClient()
        raw = {'ZumRailsType': 'AccountsPayable', 'TransactionMethod': 'Eft', 'Amount': '12.34', 'Memo': 'Test', 'UserId': ID, 'WalletId': WALLET}
        operation = prepare(provider, owner, 'create_transaction', raw)
        previous = connection.settings_dict.get('ATOMIC_REQUESTS', False)
        connection.settings_dict['ATOMIC_REQUESTS'] = True
        self.addCleanup(connection.settings_dict.__setitem__, 'ATOMIC_REQUESTS', previous)
        def uncertain(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(Operation.objects.get(pk=operation.pk).status, 'submitted')
            raise ProviderError('Uncertain provider result', unknown=True)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Real HTTP prohibited')):
            with patch.object(ZumClient, 'request', side_effect=uncertain) as request:
                response = self.client.post(f'/api/payment-processor/operations/{operation.pk}/execute/', {'payload': raw, 'confirmation': 'CONFIRM'}, format='json')
                self.assertEqual(response.status_code, 502)
                operation.refresh_from_db()
                self.assertEqual(operation.status, 'unknown')
                retry = self.client.post(f'/api/payment-processor/operations/{operation.pk}/execute/', {'payload': raw, 'confirmation': 'CONFIRM'}, format='json')
                self.assertEqual(retry.status_code, 400)
                self.assertEqual(request.call_count, 1)
        self.assertFalse(FundedPayment.objects.exists())
        self.assertFalse(CollectionPayment.objects.exists())
