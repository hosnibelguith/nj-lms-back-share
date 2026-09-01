from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

import requests
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Customer, User
from activity.models import ActivityHistory
from banking.models import BankAccount, BankConnection, BankTransaction
from banking import tasks
from loans.models import Loan


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class BankingConnectTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.login_id = str(uuid4())

        self.user_a = User.objects.create_user(
            email='banking-a@example.com',
            password='password123',
            full_name='Customer A',
            user_type='customer',
        )
        self.customer_a = Customer.objects.create(
            portal_user=self.user_a,
            first_name='Customer',
            last_name='A',
            email='banking-a@example.com',
            phone='4165550001',
            phone_normalized='4165550001',
            province='ON',
            status='pending',
            onboarding_stage='banking_verification',
        )

        self.user_b = User.objects.create_user(
            email='banking-b@example.com',
            password='password123',
            full_name='Customer B',
            user_type='customer',
        )
        self.customer_b = Customer.objects.create(
            portal_user=self.user_b,
            first_name='Customer',
            last_name='B',
            email='banking-b@example.com',
            phone='4165550002',
            phone_normalized='4165550002',
            province='ON',
            status='pending',
            onboarding_stage='banking_verification',
        )

    def _connect(self, user):
        self.client.force_authenticate(user=user)
        with patch('banking.views.fetch_flinks_accounts_only.delay') as mocked:
            response = self.client.post(
                '/api/banking/connect/',
                {'login_id': self.login_id},
                format='json',
            )
        return response, mocked

    def test_duplicate_login_id_across_customers_is_accepted(self):
        response_a, mocked_a = self._connect(self.user_a)
        self.assertEqual(response_a.status_code, 200, response_a.data)
        self.assertEqual(BankConnection.objects.filter(customer=self.customer_a).count(), 1)

        response_b, mocked_b = self._connect(self.user_b)
        self.assertEqual(response_b.status_code, 200, response_b.data)
        self.assertEqual(BankConnection.objects.filter(customer=self.customer_b).count(), 1)
        self.assertEqual(BankConnection.objects.filter(login_id=self.login_id).count(), 2)

        connection_a = BankConnection.objects.get(customer=self.customer_a)
        connection_b = BankConnection.objects.get(customer=self.customer_b)
        mocked_a.assert_called_once_with(str(connection_a.id))
        mocked_b.assert_called_once_with(str(connection_b.id))

    def test_connect_reuses_existing_synced_ibv_for_same_login(self):
        loan = Loan.objects.create(
            customer=self.customer_a,
            principal=500,
            fee=100,
            total_amount=600,
            balance=600,
            status='ibv_pending',
            is_active=True,
        )
        old_connection = BankConnection.objects.create(
            customer=self.customer_a,
            login_id=self.login_id,
            provider='flinks',
            is_active=False,
            sync_status='synced',
            last_synced_at=timezone.now(),
        )
        account = BankAccount.objects.create(
            customer=self.customer_a,
            connection=old_connection,
            external_id='acct-1',
            name='Primary Checking',
            type='checking',
            balance=100,
            is_primary=True,
        )
        BankTransaction.objects.create(
            customer=self.customer_a,
            account=account,
            external_id='tx-1',
            date=timezone.localdate(),
            description='Payroll',
            credit=100,
            balance=100,
        )
        empty_connection = BankConnection.objects.create(
            customer=self.customer_a,
            login_id=self.login_id,
            provider='flinks',
            is_active=True,
            sync_status='failed',
        )

        self.client.force_authenticate(user=self.user_a)
        with patch('banking.views.fetch_flinks_accounts_only.delay') as mocked:
            response = self.client.post(
                '/api/banking/connect/',
                {'login_id': self.login_id},
                format='json',
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], 'COMPLETED')
        mocked.assert_not_called()
        old_connection.refresh_from_db()
        empty_connection.refresh_from_db()
        self.customer_a.refresh_from_db()
        loan.refresh_from_db()
        self.assertTrue(old_connection.is_active)
        self.assertFalse(empty_connection.is_active)
        self.assertTrue(self.customer_a.banking_verified)
        self.assertEqual(self.customer_a.onboarding_stage, 'contract')
        self.assertEqual(loan.status, 'pending_signature')
        self.assertEqual(loan.bank_account_id, account.id)
        self.assertTrue(
            ActivityHistory.objects.filter(
                customer=self.customer_a,
                loan=loan,
                title='IBV Data Restored',
            ).exists()
        )

    def test_repair_command_restores_expired_loan_with_existing_ibv(self):
        loan = Loan.objects.create(
            customer=self.customer_a,
            principal=500,
            fee=100,
            total_amount=600,
            balance=600,
            status='expired',
            is_active=False,
        )
        connection = BankConnection.objects.create(
            customer=self.customer_a,
            login_id=self.login_id,
            provider='flinks',
            is_active=False,
            sync_status='synced',
            last_synced_at=timezone.now(),
        )
        account = BankAccount.objects.create(
            customer=self.customer_a,
            connection=connection,
            external_id='acct-expired',
            name='Expired Checking',
            type='checking',
            balance=100,
            is_primary=True,
        )
        BankTransaction.objects.create(
            customer=self.customer_a,
            account=account,
            external_id='tx-expired',
            date=timezone.localdate(),
            description='Payroll',
            credit=100,
            balance=100,
        )

        out = StringIO()
        call_command(
            'repair_synced_ibv',
            '--source',
            'all',
            '--email',
            self.customer_a.email,
            '--apply',
            stdout=out,
        )

        self.assertIn('Repaired 1 customer(s)', out.getvalue())
        connection.refresh_from_db()
        self.customer_a.refresh_from_db()
        loan.refresh_from_db()
        self.assertTrue(connection.is_active)
        self.assertTrue(self.customer_a.banking_verified)
        self.assertEqual(self.customer_a.onboarding_stage, 'contract')
        self.assertEqual(loan.status, 'pending_signature')
        self.assertTrue(loan.is_active)
        self.assertEqual(loan.bank_account_id, account.id)

    def test_reset_pending_connection_allows_reconnect(self):
        connection = BankConnection.objects.create(
            customer=self.customer_a,
            login_id=self.login_id,
            provider='flinks',
            is_active=True,
            sync_status='pending',
        )
        self.client.force_authenticate(user=self.user_a)
        response = self.client.post('/api/banking/reset-pending/', {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        connection.refresh_from_db()
        self.assertFalse(connection.is_active)
        self.assertEqual(connection.sync_status, 'failed')

        status = self.client.get('/api/portal/me/banking/')
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.data['has_connection'])
        self.assertFalse(status.data['banking_verified'])

    def test_reset_pending_rejected_when_already_verified(self):
        self.customer_a.banking_verified = True
        self.customer_a.save(update_fields=['banking_verified', 'updated_at'])
        BankConnection.objects.create(
            customer=self.customer_a,
            login_id=self.login_id,
            provider='flinks',
            is_active=True,
            sync_status='synced',
        )
        self.client.force_authenticate(user=self.user_a)
        response = self.client.post('/api/banking/reset-pending/', {}, format='json')
        self.assertEqual(response.status_code, 400, response.data)

    def test_sync_task_uses_connection_id_not_shared_login_id(self):
        connection_a = BankConnection.objects.create(
            customer=self.customer_a,
            login_id=self.login_id,
            provider='flinks',
            sync_status='pending',
        )
        connection_b = BankConnection.objects.create(
            customer=self.customer_b,
            login_id=self.login_id,
            provider='flinks',
            sync_status='pending',
        )

        accounts_payload = {
            'Accounts': [
                {
                    'Id': 'acct-shared',
                    'Title': 'Flinks Capital Chequing',
                    'Type': 'Chequing',
                    'Currency': 'CAD',
                    'Balance': {'Current': 1000},
                    'InstitutionNumber': '777',
                    'TransitNumber': '12345',
                    'AccountNumber': '1234567890',
                    'Transactions': [{'Id': 'tx-1', 'Date': '2026-01-01', 'Description': 'Deposit', 'Credit': 100}],
                }
            ]
        }

        with patch('banking.tasks.requests.post') as mocked_post:
            auth_response = mocked_post.return_value
            auth_response.status_code = 200
            auth_response.json.return_value = {'RequestId': 'req-1'}

            detail_response = mocked_post.return_value
            detail_response.status_code = 200
            detail_response.json.return_value = accounts_payload

            mocked_post.side_effect = [
                type('Resp', (), {'status_code': 200, 'json': lambda self: {'RequestId': 'req-1'}, 'text': ''})(),
                type('Resp', (), {'status_code': 200, 'json': lambda self: accounts_payload, 'text': ''})(),
            ]

            self.assertTrue(tasks.fetch_flinks_accounts_only(str(connection_b.id)))

        connection_b.refresh_from_db()
        self.customer_b.refresh_from_db()
        self.assertEqual(connection_b.sync_status, 'synced')
        self.assertTrue(self.customer_b.banking_verified)
        self.assertEqual(BankAccount.objects.filter(customer=self.customer_b).count(), 1)

        connection_a.refresh_from_db()
        self.customer_a.refresh_from_db()
        self.assertEqual(connection_a.sync_status, 'pending')
        self.assertFalse(self.customer_a.banking_verified)

    def _sync_payload_with_institution(self, institution_number):
        connection = BankConnection.objects.create(
            customer=self.customer_a,
            login_id=str(uuid4()),
            provider='flinks',
            sync_status='pending',
        )
        accounts_payload = {
            'Accounts': [
                {
                    'Id': 'acct-621',
                    'Title': 'Test Chequing',
                    'Type': 'Chequing',
                    'Currency': 'CAD',
                    'Balance': {'Current': 500},
                    'InstitutionNumber': institution_number,
                    'TransitNumber': '12345',
                    'AccountNumber': '9999999999',
                    'Transactions': [{
                        'Id': f'tx-{institution_number}',
                        'Date': '2026-01-01',
                        'Description': 'Pay',
                        'Credit': 50,
                    }],
                }
            ]
        }

        with patch('banking.tasks.requests.post') as mocked_post:
            mocked_post.side_effect = [
                type(
                    'Resp',
                    (),
                    {
                        'status_code': 200,
                        'json': lambda self: {'RequestId': f'req-{institution_number}'},
                        'text': '',
                    },
                )(),
                type('Resp', (), {'status_code': 200, 'json': lambda self: accounts_payload, 'text': ''})(),
            ]
            result = tasks.fetch_flinks_accounts_only(str(connection.id))

        return connection, result

    def test_institution_621_is_saved_like_any_other_bank(self):
        connection, result = self._sync_payload_with_institution('621')

        self.customer_a.refresh_from_db()
        self.assertTrue(result)
        self.assertTrue(BankConnection.objects.filter(id=connection.id).exists())
        account = BankAccount.objects.get(customer=self.customer_a, institution_number='621')
        self.assertTrue(account.is_payment_blocked)
        self.assertTrue(self.customer_a.banking_verified)
        self.assertFalse(
            ActivityHistory.objects.filter(
                customer=self.customer_a,
                metadata__reason_code=tasks.UNSUPPORTED_IBV_REASON_CODE,
            ).exists()
        )

        self.client.force_authenticate(user=self.user_a)
        response = self.client.get('/api/portal/me/banking/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data['requires_ibv_refill'])

    def test_institution_623_is_saved_like_any_other_bank(self):
        connection, result = self._sync_payload_with_institution('623')

        self.customer_a.refresh_from_db()
        self.assertTrue(result)
        self.assertTrue(BankConnection.objects.filter(id=connection.id).exists())
        account = BankAccount.objects.get(customer=self.customer_a, institution_number='623')
        self.assertTrue(account.is_payment_blocked)
        self.assertTrue(self.customer_a.banking_verified)

    def test_flinks_persists_account_number_longer_than_twenty_chars(self):
        long_number = "218122623398012345678901"
        self.assertGreater(len(long_number), 20)
        connection = BankConnection.objects.create(
            customer=self.customer_a,
            login_id=str(uuid4()),
            provider="flinks",
            is_active=True,
            sync_status="pending",
        )

        tasks._persist_accounts(
            connection,
            self.customer_a,
            [
                {
                    "Id": "acct-long-number",
                    "Title": "Chequing",
                    "Type": "Chequing",
                    "Currency": "CAD",
                    "InstitutionNumber": "003",
                    "TransitNumber": "12345",
                    "AccountNumber": long_number,
                    "Transactions": [],
                }
            ],
        )

        account = BankAccount.objects.get(
            connection=connection, external_id="acct-long-number"
        )
        self.assertEqual(account.account_number, long_number)
        self.assertGreaterEqual(
            BankAccount._meta.get_field("account_number").max_length,
            len(long_number),
        )

    def test_flinks_authorize_requests_fresh_detail_not_stale_cache(self):
        connection = BankConnection.objects.create(
            customer=self.customer_a,
            login_id=str(uuid4()),
            provider='flinks',
            sync_status='pending',
        )
        accounts_payload = {
            'Accounts': [
                {
                    'Id': 'acct-fresh',
                    'Title': 'KOHO',
                    'Type': 'Chequing',
                    'Currency': 'CAD',
                    'InstitutionNumber': '621',
                    'TransitNumber': '16001',
                    'AccountNumber': '218122623398',
                    'Transactions': [
                        {
                            'Id': 'tx-1',
                            'Date': '2026-08-01',
                            'Description': 'Pay',
                            'Credit': 10,
                        }
                    ],
                }
            ]
        }
        with patch('banking.tasks.requests.post') as mocked_post:
            mocked_post.side_effect = [
                type(
                    'Resp',
                    (),
                    {
                        'status_code': 200,
                        'json': lambda self: {'RequestId': 'req-fresh'},
                        'text': '',
                    },
                )(),
                type(
                    'Resp',
                    (),
                    {
                        'status_code': 200,
                        'json': lambda self: accounts_payload,
                        'text': '',
                    },
                )(),
            ]
            self.assertTrue(tasks.fetch_flinks_accounts_only(str(connection.id)))
            auth_kwargs = mocked_post.call_args_list[0].kwargs
            self.assertFalse(auth_kwargs['json']['MostRecentCached'])

    def test_flinks_zero_tx_retry_then_succeeds(self):
        connection = BankConnection.objects.create(
            customer=self.customer_a,
            login_id=str(uuid4()),
            provider='flinks',
            sync_status='pending',
        )
        empty_payload = {
            'Accounts': [
                {
                    'Id': 'acct-empty',
                    'Title': 'KOHO',
                    'Type': 'Chequing',
                    'Currency': 'CAD',
                    'InstitutionNumber': '621',
                    'TransitNumber': '16001',
                    'AccountNumber': '218122623398',
                    'Transactions': [],
                }
            ]
        }
        filled_payload = {
            'Accounts': [
                {
                    'Id': 'acct-empty',
                    'Title': 'KOHO',
                    'Type': 'Chequing',
                    'Currency': 'CAD',
                    'InstitutionNumber': '621',
                    'TransitNumber': '16001',
                    'AccountNumber': '218122623398',
                    'Transactions': [
                        {
                            'Id': 'tx-later',
                            'Date': '2026-08-01',
                            'Description': 'Deposit',
                            'Credit': 25,
                        }
                    ],
                }
            ]
        }

        def _resp(payload):
            return type(
                'Resp',
                (),
                {
                    'status_code': 200,
                    'json': lambda self, p=payload: p,
                    'text': '',
                },
            )()

        with patch('banking.tasks.time.sleep'), patch(
            'banking.tasks.requests.post'
        ) as mocked_post:
            mocked_post.side_effect = [
                _resp({'RequestId': 'req-1'}),
                _resp(empty_payload),
                _resp({'RequestId': 'req-2'}),
                _resp(filled_payload),
            ]
            self.assertTrue(tasks.fetch_flinks_accounts_only(str(connection.id)))

        connection.refresh_from_db()
        self.customer_a.refresh_from_db()
        self.assertEqual(connection.sync_status, 'synced')
        self.assertTrue(self.customer_a.banking_verified)
        self.assertEqual(
            BankTransaction.objects.filter(customer=self.customer_a).count(),
            1,
        )

    def test_purge_command_is_noop_when_no_auto_reject_institutions(self):
        self.customer_a.banking_verified = True
        self.customer_a.onboarding_stage = 'contract'
        self.customer_a.save(update_fields=['banking_verified', 'onboarding_stage', 'updated_at'])
        connection = BankConnection.objects.create(
            customer=self.customer_a,
            login_id=str(uuid4()),
            provider='flinks',
            is_active=True,
            sync_status='synced',
        )
        BankAccount.objects.create(
            connection=connection,
            customer=self.customer_a,
            external_id='acct-stale-621',
            name='Risk Chequing',
            type='checking',
            institution_number='621',
            transit_number='12345',
            account_number='9999999999',
        )

        output = StringIO()
        call_command('purge_unsupported_ibv_connections', stdout=output)

        self.customer_a.refresh_from_db()
        self.assertTrue(BankConnection.objects.filter(id=connection.id).exists())
        self.assertEqual(BankAccount.objects.filter(customer=self.customer_a).count(), 1)
        self.assertTrue(self.customer_a.banking_verified)
        self.assertIn('0 unsupported IBV connection', output.getvalue())


@override_settings(
    MOHAWK_BANKING_ANALYSIS_API_KEY='test-mohawk-key',
)
class MohawkBankingAnalysisWebhookTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.login_id = 'full-stack-test-login'
        self.user = User.objects.create_user(
            email='mohawk-webhook@example.com',
            password='password123',
            full_name='Mohawk Customer',
            user_type='customer',
        )
        self.customer = Customer.objects.create(
            portal_user=self.user,
            first_name='Mohawk',
            last_name='Customer',
            email='mohawk-webhook@example.com',
            phone='4165550099',
            phone_normalized='4165550099',
            province='ON',
            status='pending',
            onboarding_stage='banking_verification',
        )
        self.connection = BankConnection.objects.create(
            customer=self.customer,
            login_id=self.login_id,
            provider='flinks',
            is_active=True,
            sync_status='synced',
        )
        self.first_account = BankAccount.objects.create(
            connection=self.connection,
            customer=self.customer,
            external_id='acct-621',
            name='Unsupported 621',
            type='checking',
            institution_number='621',
            transit_number='11111',
            account_number='1111111',
            is_primary=True,
        )
        self.second_account = BankAccount.objects.create(
            connection=self.connection,
            customer=self.customer,
            external_id='acct-primary',
            name='Operational Chequing',
            type='checking',
            institution_number='001',
            transit_number='12345',
            account_number='1234567',
            is_primary=False,
        )

    def _payload(self, event_id='lendstack-mohawk-analysis-test-1'):
        return {
            'schema_version': '1.0',
            'event': 'banking_analysis.completed',
            'event_id': event_id,
            'report_id': 4821,
            'login_id': self.login_id,
            'tag': 'Mohawk',
            'decision_1': {'decision': 'APPROVE', 'reason': 'ok', 'approved_terms': 'N/A', 'repayment_suggestions': 'N/A'},
            'decision_2': {'decision': 'APPROVE', 'reason': None},
            'primary_bank_account': {
                'institution_number': '001',
                'transit_number': '12345',
                'account_number': '1234567',
            },
            'report': {'affordability': {'status': 'PASS'}},
            'final_report_text': '**FINAL DECISION: APPROVE**',
            'source_transactions': [],
        }

    def _post(self, payload, api_key='test-mohawk-key'):
        return self.client.post(
            '/api/integrations/mohawk/banking-analysis/',
            payload,
            format='json',
            HTTP_AUTHORIZATION=f'Token {api_key}',
            HTTP_X_EVENT_ID=payload.get('event_id', ''),
        )

    def test_rejects_invalid_api_key(self):
        response = self._post(self._payload(), api_key='bad-key')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data['error'], 'invalid_api_key')

    def test_sets_primary_eft_account_and_is_idempotent(self):
        payload = self._payload()
        first = self._post(payload)
        self.assertEqual(first.status_code, 201, first.data)
        self.assertFalse(first.data['duplicate'])

        self.first_account.refresh_from_db()
        self.second_account.refresh_from_db()
        self.assertFalse(self.first_account.is_primary)
        self.assertFalse(self.first_account.use_for_eft_funding)
        self.assertTrue(self.second_account.is_primary)
        self.assertTrue(self.second_account.use_for_eft_funding)
        self.assertTrue(self.second_account.use_for_eft_collections)

        second = self._post(payload)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertTrue(second.data['duplicate'])
        from banking.models import BankingAnalysisEvent
        self.assertEqual(BankingAnalysisEvent.objects.filter(event_id=payload['event_id']).count(), 1)


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
)
class ManualBankAccountStaffTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            email='staff-bank@example.com',
            password='password123',
            full_name='Staff User',
            user_type='staff',
            is_staff=True,
        )
        self.portal = User.objects.create_user(
            email='void-cheque@example.com',
            password='password123',
            full_name='Void Cheque Customer',
            user_type='customer',
        )
        self.customer = Customer.objects.create(
            portal_user=self.portal,
            first_name='Void',
            last_name='Cheque',
            email='void-cheque@example.com',
            phone='4165550099',
            phone_normalized='4165550099',
            province='ON',
            status='pending',
        )
        self.connection = BankConnection.objects.create(
            customer=self.customer,
            login_id='flinks-login-1',
            provider='flinks',
            is_active=True,
            sync_status='synced',
        )
        self.account = BankAccount.objects.create(
            connection=self.connection,
            customer=self.customer,
            external_id='flinks-acct-1',
            name='Chequing',
            type='checking',
            balance=Decimal('100.00'),
            transit_number='11111',
            institution_number='001',
            account_number='1111111',
            is_primary=True,
        )
        self.client.force_authenticate(user=self.staff)

    def test_staff_can_update_coordinates_from_void_cheque(self):
        response = self.client.patch(
            f'/api/bank-accounts/{self.account.id}/coordinates/',
            {
                'institution_number': '004',
                'transit_number': '12345',
                'account_number': '987654321',
                'notes': 'New void emailed by customer',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.account.refresh_from_db()
        self.assertEqual(self.account.institution_number, '004')
        self.assertEqual(self.account.transit_number, '12345')
        self.assertEqual(self.account.account_number, '987654321')
        self.assertTrue(self.account.is_manual_entry)
        self.assertTrue(
            ActivityHistory.objects.filter(
                customer=self.customer,
                title='Bank details updated (void cheque)',
            ).exists()
        )

    def test_update_coordinates_rejects_invalid_institution(self):
        response = self.client.patch(
            f'/api/bank-accounts/{self.account.id}/coordinates/',
            {
                'institution_number': '12',
                'transit_number': '12345',
                'account_number': '987654321',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_staff_can_create_manual_account_from_void_cheque(self):
        response = self.client.post(
            '/api/bank-accounts/manual/',
            {
                'customer_id': str(self.customer.id),
                'institution_number': '003',
                'transit_number': '54321',
                'account_number': '5555555',
                'name': 'Void Cheque Account',
                'notes': 'Received by email',
                'set_as_primary': True,
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data['is_manual_entry'])
        self.assertEqual(response.data['institution_number'], '003')
        self.account.refresh_from_db()
        self.assertFalse(self.account.is_primary)
        manual = BankAccount.objects.get(id=response.data['id'])
        self.assertTrue(manual.is_primary)
        self.assertEqual(manual.connection.provider, 'manual')
        self.assertTrue(
            ActivityHistory.objects.filter(
                customer=self.customer,
                title='Manual bank account (void cheque)',
            ).exists()
        )

    def test_bank_accounts_return_transactions_newest_first(self):
        BankTransaction.objects.create(
            account=self.account,
            customer=self.customer,
            external_id='tx-old',
            date='2026-01-01',
            description='Older txn',
            credit=Decimal('10.00'),
            balance=Decimal('110.00'),
        )
        BankTransaction.objects.create(
            account=self.account,
            customer=self.customer,
            external_id='tx-new',
            date='2026-03-15',
            description='Newer txn',
            debit=Decimal('5.00'),
            balance=Decimal('105.00'),
        )
        BankTransaction.objects.create(
            account=self.account,
            customer=self.customer,
            external_id='tx-mid',
            date='2026-02-10',
            description='Middle txn',
            credit=Decimal('1.00'),
            balance=Decimal('111.00'),
        )

        response = self.client.get(f'/api/bank-accounts/?customer_id={self.customer.id}')
        self.assertEqual(response.status_code, 200, response.data)
        payload = response.data if isinstance(response.data, list) else response.data.get('results', [])
        account_payload = next(item for item in payload if item['id'] == str(self.account.id))
        dates = [txn['date'] for txn in account_payload['transactions']]
        self.assertEqual(dates, ['2026-03-15', '2026-02-10', '2026-01-01'])
        self.assertEqual(account_payload['institution_number'], '001')
        self.assertEqual(account_payload['transit_number'], '11111')
        self.assertEqual(account_payload['account_number'], '1111111')
        self.assertFalse(account_payload['is_payment_blocked'])

    def test_void_cheque_entry_allows_risk_institutions_with_flag(self):
        for institution in ('621', '623', '703'):
            with self.subTest(institution=institution):
                update = self.client.patch(
                    f'/api/bank-accounts/{self.account.id}/coordinates/',
                    {
                        'institution_number': institution,
                        'transit_number': '12345',
                        'account_number': '987654321',
                    },
                    format='json',
                )
                self.assertEqual(update.status_code, 200, update.data)
                self.assertTrue(update.data.get('is_payment_blocked'))

                create = self.client.post(
                    '/api/bank-accounts/manual/',
                    {
                        'customer_id': str(self.customer.id),
                        'institution_number': institution,
                        'transit_number': '54321',
                        'account_number': '5555555',
                    },
                    format='json',
                )
                self.assertEqual(create.status_code, 201, create.data)
                self.assertTrue(create.data.get('is_payment_blocked'))

        self.account.refresh_from_db()
        self.assertEqual(self.account.institution_number, '703')


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    MOHAWK_BANKING_ANALYSIS_API_KEY='test-mohawk-key',
)
class BlockedInstitutionBankingTests(TestCase):
    """703 is accepted for IBV; agents may fund/collect after verifying PAD history."""

    def setUp(self):
        self.client = APIClient()
        self.login_id = str(uuid4())
        self.portal = User.objects.create_user(
            email='blocked-banking@example.com',
            password='password123',
            full_name='Blocked Banking',
            user_type='customer',
        )
        self.customer = Customer.objects.create(
            portal_user=self.portal,
            first_name='Blocked',
            last_name='Banking',
            email='blocked-banking@example.com',
            phone='4165550088',
            phone_normalized='4165550088',
            province='ON',
            status='pending',
        )
        self.connection = BankConnection.objects.create(
            customer=self.customer,
            login_id=self.login_id,
            provider='flinks',
            is_active=True,
            sync_status='synced',
        )

    def test_flinks_sync_prefers_non_risk_primary_but_flags_risk_account(self):
        tasks._persist_accounts(
            self.connection,
            self.customer,
            [
                {
                    'Id': 'acct-703',
                    'Title': 'Risk 703',
                    'Type': 'Chequing',
                    'Currency': 'CAD',
                    'InstitutionNumber': '703',
                    'TransitNumber': '11111',
                    'AccountNumber': '1111111',
                    'Transactions': [],
                },
                {
                    'Id': 'acct-001',
                    'Title': 'Operational Chequing',
                    'Type': 'Chequing',
                    'Currency': 'CAD',
                    'InstitutionNumber': '001',
                    'TransitNumber': '12345',
                    'AccountNumber': '1234567',
                    'Transactions': [],
                },
            ],
        )

        risk = BankAccount.objects.get(connection=self.connection, external_id='acct-703')
        allowed = BankAccount.objects.get(connection=self.connection, external_id='acct-001')
        self.assertFalse(risk.is_primary)
        self.assertTrue(risk.is_payment_blocked)
        self.assertTrue(allowed.is_primary)

    def test_flinks_sync_allows_risk_only_account_as_primary(self):
        tasks._persist_accounts(
            self.connection,
            self.customer,
            [
                {
                    'Id': 'acct-703-only',
                    'Title': 'Only 703',
                    'Type': 'Chequing',
                    'Currency': 'CAD',
                    'InstitutionNumber': '703',
                    'TransitNumber': '11111',
                    'AccountNumber': '1111111',
                    'Transactions': [],
                },
            ],
        )
        risk = BankAccount.objects.get(connection=self.connection, external_id='acct-703-only')
        self.assertTrue(risk.is_primary)
        self.assertTrue(risk.is_payment_blocked)

    def test_mohawk_webhook_applies_risk_primary_with_warning_note(self):
        response = self.client.post(
            '/api/integrations/mohawk/banking-analysis/',
            {
                'schema_version': '1.0',
                'event': 'banking_analysis.completed',
                'event_id': 'blocked-primary-1',
                'login_id': self.login_id,
                'tag': 'Mohawk',
                'primary_bank_account': {
                    'institution_number': '703',
                    'transit_number': '12345',
                    'account_number': '1234567',
                },
            },
            format='json',
            HTTP_AUTHORIZATION='Token test-mohawk-key',
        )

        self.assertEqual(response.status_code, 201, response.data)
        from banking.models import BankingAnalysisEvent

        event = BankingAnalysisEvent.objects.get(event_id='blocked-primary-1')
        self.assertFalse(event.eft_setup_incomplete)
        self.assertIn('703', event.exception_note)
        self.assertIsNotNone(event.primary_account)
        self.assertTrue(
            BankAccount.objects.filter(
                connection=self.connection,
                use_for_eft_funding=True,
            ).exists()
        )
        self.assertTrue(
            BankAccount.objects.filter(
                connection=self.connection,
                use_for_eft_collections=True,
            ).exists()
        )


@override_settings(MOHAWK_BANKING_ANALYSIS_API_KEY='test-mohawk-key')
class MohawkAnalysisIbvHealTests(TestCase):
    """Mohawk analysis completes IBV when Flinks sync left the loan stuck."""

    def setUp(self):
        from loans.models import Loan

        self.client = APIClient()
        self.login_id = '6c776d4b-9ed5-4b37-f377-08def7e2a828'
        self.user = User.objects.create_user(
            email='klevis@example.com',
            password='password123',
            full_name='Klevis Prendi',
            user_type='customer',
        )
        self.customer = Customer.objects.create(
            portal_user=self.user,
            first_name='Klevis',
            last_name='Prendi',
            email='zag448126@gmail.com',
            phone='4168814077',
            phone_normalized='4168814077',
            province='ON',
            status='pending',
            onboarding_stage='banking_verification',
            banking_verified=False,
            source='arrive',
            requested_loan_amount=Decimal('500.00'),
        )
        self.connection = BankConnection.objects.create(
            customer=self.customer,
            login_id=self.login_id,
            provider='flinks',
            is_active=True,
            sync_status='failed',
            sync_error='We could not retrieve transaction history',
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal('500.00'),
            fee=Decimal('100.00'),
            total_amount=Decimal('600.00'),
            balance=Decimal('600.00'),
            status='ibv_pending',
            is_active=True,
        )

    def test_mohawk_analysis_advances_stuck_ibv_pending_loan(self):
        response = self.client.post(
            '/api/integrations/mohawk/banking-analysis/',
            {
                'schema_version': '1.0',
                'event': 'banking_analysis.completed',
                'event_id': 'klevis-ibv-heal-1',
                'login_id': self.login_id,
                'tag': 'Mohawk',
                'primary_bank_account': {
                    'institution_number': '621',
                    'transit_number': '16001',
                    'account_number': '218122623398',
                },
                'source_transactions': [{'Id': f'tx-{i}'} for i in range(25)],
                'report': {},
                'final_report_text': 'review',
            },
            format='json',
            HTTP_AUTHORIZATION='Token test-mohawk-key',
        )
        self.assertEqual(response.status_code, 201, response.data)

        self.customer.refresh_from_db()
        self.connection.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertTrue(self.customer.banking_verified)
        self.assertEqual(self.customer.onboarding_stage, 'contract')
        self.assertEqual(self.connection.sync_status, 'synced')
        self.assertEqual(self.loan.status, 'pending_signature')
        self.assertTrue(
            ActivityHistory.objects.filter(
                customer=self.customer,
                title='Banking Verification Completed',
                metadata__source='mohawk_banking_analysis',
            ).exists()
        )
        self.user.refresh_from_db()
        self.assertFalse(self.user.flinks_email)
        self.assertFalse(self.user.flinks_name)

    def test_mohawk_analysis_saves_flinks_holder_identity(self):
        with patch('banking.tasks.fetch_flinks_accounts_only.delay'):
            response = self.client.post(
                '/api/integrations/mohawk/banking-analysis/',
                {
                    'schema_version': '1.0',
                    'event': 'banking_analysis.completed',
                    'event_id': 'adolfo-ibv-identity-1',
                    'login_id': self.login_id,
                    'tag': 'Mohawk',
                    'holder': {
                        'Name': 'ADOLFO CUEVAS',
                        'Email': 'adcuevasrios@gmail.com',
                    },
                    'primary_bank_account': {
                        'institution_number': '002',
                        'transit_number': '20081',
                        'account_number': '1110586',
                        'holder_name': 'ADOLFO CUEVAS',
                    },
                    'source_transactions': [{'Id': 'tx-1'}],
                    'report': {},
                    'final_report_text': 'review',
                },
                format='json',
                HTTP_AUTHORIZATION='Token test-mohawk-key',
            )
        self.assertEqual(response.status_code, 201, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.flinks_name, 'ADOLFO CUEVAS')
        self.assertEqual(self.user.flinks_email, 'adcuevasrios@gmail.com')

    def test_mohawk_analysis_backfills_identity_when_already_verified(self):
        self.customer.banking_verified = True
        self.customer.onboarding_stage = 'contract'
        self.customer.save(
            update_fields=['banking_verified', 'onboarding_stage', 'updated_at']
        )
        self.connection.sync_status = 'synced'
        self.connection.save(update_fields=['sync_status', 'updated_at'])
        self.loan.status = 'pending_signature'
        self.loan.save(update_fields=['status', 'updated_at'])

        with patch('banking.tasks.fetch_flinks_accounts_only.delay') as mocked:
            response = self.client.post(
                '/api/integrations/mohawk/banking-analysis/',
                {
                    'schema_version': '1.0',
                    'event': 'banking_analysis.completed',
                    'event_id': 'arrive-identity-backfill-1',
                    'login_id': self.login_id,
                    'tag': 'Mohawk',
                    'holder': {
                        'Name': 'ADOLFO CUEVAS',
                        'Email': 'adcuevasrios@gmail.com',
                    },
                    'primary_bank_account': {
                        'institution_number': '002',
                        'transit_number': '20081',
                        'account_number': '1110586',
                    },
                    'source_transactions': [{'Id': 'tx-1'}],
                    'report': {},
                    'final_report_text': 'review',
                },
                format='json',
                HTTP_AUTHORIZATION='Token test-mohawk-key',
            )
        self.assertEqual(response.status_code, 201, response.data)
        self.user.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(self.user.flinks_name, 'ADOLFO CUEVAS')
        self.assertEqual(self.user.flinks_email, 'adcuevasrios@gmail.com')
        self.assertTrue(self.customer.banking_verified)
        mocked.assert_not_called()


class FlinksHolderIdentityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='holder-id@example.com',
            password='password123',
            full_name='Holder Customer',
            user_type='customer',
        )
        self.customer = Customer.objects.create(
            portal_user=self.user,
            first_name='Holder',
            last_name='Customer',
            email='holder-id@example.com',
            phone='4165550499',
            phone_normalized='4165550499',
            province='ON',
            status='pending',
            onboarding_stage='banking_verification',
            banking_verified=False,
        )
        self.connection = BankConnection.objects.create(
            customer=self.customer,
            login_id=str(uuid4()),
            provider='flinks',
            is_active=True,
            sync_status='pending',
        )

    def test_extracts_emails_list_and_first_last_name(self):
        email, phone, name = tasks._extract_holder_identity(
            [
                {
                    'Holder': {
                        'FirstName': 'Jacob',
                        'LastName': 'Boos',
                        'Emails': [{'Address': 'boosevolutionltd@gmail.com'}],
                        'PhoneNumber': '4033970975',
                    },
                    'Transactions': [{'Id': 'tx-1'}],
                }
            ]
        )
        self.assertEqual(email, 'boosevolutionltd@gmail.com')
        self.assertEqual(name, 'Jacob Boos')
        self.assertEqual(phone, '4033970975')

    def test_gad_after_verified_saves_identity_without_unverify(self):
        self.customer.banking_verified = True
        self.customer.save(update_fields=['banking_verified', 'updated_at'])
        self.connection.sync_status = 'synced'
        self.connection.save(update_fields=['sync_status', 'updated_at'])

        ok = tasks.apply_flinks_accounts_detail(
            self.connection,
            {
                'Accounts': [
                    {
                        'Id': 'acct-arrive-1',
                        'Title': 'Preferred Package',
                        'Type': 'Chequing',
                        'Currency': 'CAD',
                        'InstitutionNumber': '002',
                        'TransitNumber': '20081',
                        'AccountNumber': '1110586',
                        'Holder': {
                            'Name': 'ADOLFO CUEVAS',
                            'Email': 'adcuevasrios@gmail.com',
                        },
                        'Transactions': [],
                    }
                ]
            },
        )
        self.assertTrue(ok)
        self.customer.refresh_from_db()
        self.user.refresh_from_db()
        self.connection.refresh_from_db()
        self.assertTrue(self.customer.banking_verified)
        self.assertEqual(self.connection.sync_status, 'synced')
        self.assertEqual(self.user.flinks_name, 'ADOLFO CUEVAS')
        self.assertEqual(self.user.flinks_email, 'adcuevasrios@gmail.com')
        self.assertTrue(
            BankAccount.objects.filter(
                customer=self.customer, external_id='acct-arrive-1'
            ).exists()
        )

    def test_gad_zero_transactions_still_fails_unverified_ibv(self):
        with patch('banking.tasks.send_banking_retry_email.delay'):
            ok = tasks.apply_flinks_accounts_detail(
                self.connection,
                {
                    'Accounts': [
                        {
                            'Id': 'acct-empty',
                            'Title': 'Chequing',
                            'Type': 'Chequing',
                            'Holder': {'Name': 'No Tx'},
                            'Transactions': [],
                        }
                    ]
                },
            )
        self.assertFalse(ok)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.banking_verified)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_status, 'failed')


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class FlinksWebhookAndTimeoutTests(TestCase):
    """GetAccountsDetail webhook is source of truth when Authorize pull times out."""

    def setUp(self):
        from loans.models import Loan

        self.client = APIClient()
        self.login_id = str(uuid4())
        self.user = User.objects.create_user(
            email='flinks-webhook@example.com',
            password='password123',
            full_name='Webhook Customer',
            user_type='customer',
        )
        self.customer = Customer.objects.create(
            portal_user=self.user,
            first_name='Webhook',
            last_name='Customer',
            email='flinks-webhook@example.com',
            phone='4165550199',
            phone_normalized='4165550199',
            province='ON',
            status='pending',
            onboarding_stage='banking_verification',
            banking_verified=False,
        )
        self.connection = BankConnection.objects.create(
            customer=self.customer,
            login_id=self.login_id,
            provider='flinks',
            is_active=True,
            sync_status='pending',
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal('500.00'),
            fee=Decimal('100.00'),
            total_amount=Decimal('600.00'),
            balance=Decimal('600.00'),
            status='ibv_pending',
            is_active=True,
        )

    def _accounts_payload(self):
        return {
            'ResponseType': 'GetAccountsDetail',
            'HttpStatusCode': 200,
            'Accounts': [
                {
                    'Id': 'acct-webhook-1',
                    'Title': 'Home',
                    'Type': 'Chequing',
                    'Currency': 'CAD',
                    'InstitutionNumber': '001',
                    'TransitNumber': '12345',
                    'AccountNumber': '9999999',
                    'Holder': {
                        'Name': 'Webhook Customer',
                        'Email': 'flinks-webhook@example.com',
                        'PhoneNumber': '4165550199',
                    },
                    'Transactions': [
                        {
                            'Id': 'tx-wh-1',
                            'Date': '2026-08-01',
                            'Description': 'Payroll',
                            'Credit': 1000,
                        }
                    ],
                }
            ],
            'Login': {'Id': self.login_id, 'Username': 'user'},
            'RequestId': 'req-webhook-1',
        }

    def test_flinks_get_accounts_detail_webhook_completes_ibv(self):
        response = self.client.post(
            '/api/webhooks/flinks/',
            self._accounts_payload(),
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], 'synced')

        self.customer.refresh_from_db()
        self.connection.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertTrue(self.customer.banking_verified)
        self.assertEqual(self.customer.onboarding_stage, 'contract')
        self.assertEqual(self.connection.sync_status, 'synced')
        self.assertEqual(self.loan.status, 'pending_signature')
        self.assertEqual(
            BankAccount.objects.filter(customer=self.customer).count(),
            1,
        )
        self.assertEqual(
            BankTransaction.objects.filter(customer=self.customer).count(),
            1,
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.flinks_email, 'flinks-webhook@example.com')
        self.assertEqual(self.user.flinks_name, 'Webhook Customer')

    def test_flinks_kyc_webhook_is_ignored(self):
        response = self.client.post(
            '/api/webhooks/flinks/',
            {
                'ResponseType': 'KYC',
                'Login': {'Id': self.login_id},
                'Accounts': [],
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'ignored')
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.banking_verified)

    def test_authorize_timeout_awaits_webhook_without_verification_failed(self):
        with patch.object(tasks.fetch_flinks_accounts_only, 'max_retries', 0), patch(
            'banking.tasks.fetch_flinks_accounts_only.apply_async'
        ) as scheduled, patch(
            'banking.tasks.fetch_flinks_accounts_only.delay'
        ) as delayed, patch(
            'banking.tasks.requests.post',
            side_effect=requests.exceptions.ReadTimeout(
                "HTTPSConnectionPool(host='alphaloans-ca-api.private.fin.ag', port=443): "
                'Read timed out. (read timeout=30)'
            ),
        ):
            result = tasks.fetch_flinks_accounts_only(str(self.connection.id))

        self.assertFalse(result)
        scheduled.assert_not_called()
        delayed.assert_not_called()
        self.connection.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(self.connection.sync_status, 'pending')
        self.assertEqual(self.connection.attempted_syncs, 1)
        self.assertIsNone(self.connection.last_synced_at)
        self.assertIn('awaiting GetAccountsDetail webhook', self.connection.sync_error)
        self.assertFalse(
            ActivityHistory.objects.filter(
                customer=self.customer,
                title='IBV Re-pull Started',
            ).exists()
        )
        self.assertFalse(self.customer.banking_verified)
        self.assertFalse(
            ActivityHistory.objects.filter(
                customer=self.customer,
                title='Banking Verification Failed',
            ).exists()
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_timeout_then_flinks_webhook_recovers_ibv(self):
        with patch.object(tasks.fetch_flinks_accounts_only, 'max_retries', 0), patch(
            'banking.tasks.fetch_flinks_accounts_only.apply_async'
        ), patch(
            'banking.tasks.requests.post',
            side_effect=requests.exceptions.ReadTimeout('Read timed out'),
        ):
            tasks.fetch_flinks_accounts_only(str(self.connection.id))

        response = self.client.post(
            '/api/webhooks/flinks/',
            self._accounts_payload(),
            format='json',
        )
        self.assertEqual(response.data['status'], 'synced')
        self.customer.refresh_from_db()
        self.connection.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertTrue(self.customer.banking_verified)
        self.assertEqual(self.connection.sync_status, 'synced')
        self.assertEqual(self.loan.status, 'pending_signature')

    def test_timeout_heals_from_existing_mohawk_analysis(self):
        from banking.models import BankingAnalysisEvent

        BankingAnalysisEvent.objects.create(
            event_id='timeout-heal-mohawk-1',
            event='banking_analysis.completed',
            schema_version='1.0',
            login_id=self.login_id,
            tag='Mohawk',
            connection=self.connection,
            customer=self.customer,
            primary_bank_account={
                'institution_number': '001',
                'transit_number': '12345',
                'account_number': '9999999',
            },
            source_transactions=[{'Id': 'tx-1'}],
            processing_status='accepted',
        )

        with patch.object(tasks.fetch_flinks_accounts_only, 'max_retries', 0), patch(
            'banking.tasks.fetch_flinks_accounts_only.apply_async'
        ) as scheduled, patch(
            'banking.tasks.requests.post',
            side_effect=requests.exceptions.ReadTimeout('Read timed out'),
        ):
            result = tasks.fetch_flinks_accounts_only(str(self.connection.id))

        self.assertTrue(result)
        scheduled.assert_not_called()
        self.customer.refresh_from_db()
        self.connection.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertTrue(self.customer.banking_verified)
        self.assertEqual(self.connection.sync_status, 'synced')
        self.assertEqual(self.loan.status, 'pending_signature')

    def test_authorize_203_mfa_does_not_mark_verification_failed(self):
        challenge = type(
            'Resp',
            (),
            {
                'status_code': 203,
                'text': '{"HttpStatusCode":203,"SecurityChallenges":[{"Type":"SMS"}]}',
                'json': lambda self: {
                    'HttpStatusCode': 203,
                    'SecurityChallenges': [{'Type': 'SMS'}],
                },
            },
        )()
        with patch.object(tasks.fetch_flinks_accounts_only, 'max_retries', 0), patch(
            'banking.tasks.fetch_flinks_accounts_only.apply_async'
        ) as scheduled, patch(
            'banking.tasks.requests.post',
            return_value=challenge,
        ):
            result = tasks.fetch_flinks_accounts_only(str(self.connection.id))

        self.assertFalse(result)
        scheduled.assert_not_called()
        self.connection.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(self.connection.sync_status, 'pending')
        self.assertEqual(self.connection.attempted_syncs, 1)
        self.assertIn('awaiting GetAccountsDetail webhook', self.connection.sync_error)
        self.assertFalse(self.customer.banking_verified)
        self.assertFalse(
            ActivityHistory.objects.filter(
                customer=self.customer,
                title='Banking Verification Failed',
            ).exists()
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_timeout_recovers_from_cached_flinks_detail(self):
        live_timeout = requests.exceptions.ReadTimeout('Read timed out')
        payload = self._accounts_payload()
        cached_auth = type(
            'Resp',
            (),
            {
                'status_code': 200,
                'text': '',
                'json': lambda self: {'RequestId': 'cached-req'},
            },
        )()
        cached_detail = type(
            'Resp',
            (),
            {
                'status_code': 200,
                'text': '',
                'json': lambda self, p=payload: p,
            },
        )()

        with patch.object(tasks.fetch_flinks_accounts_only, 'max_retries', 0), patch(
            'banking.tasks.fetch_flinks_accounts_only.apply_async'
        ) as scheduled, patch(
            'banking.tasks.requests.post',
            side_effect=[live_timeout, cached_auth, cached_detail],
        ):
            result = tasks.fetch_flinks_accounts_only(str(self.connection.id))

        scheduled.assert_not_called()

        self.assertTrue(result)
        self.customer.refresh_from_db()
        self.connection.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertTrue(self.customer.banking_verified)
        self.assertEqual(self.connection.sync_status, 'synced')
        self.assertEqual(self.loan.status, 'pending_signature')


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class FlinksGadRepullTests(TestCase):
    """Staff/portal can restart GetAccountsDetail without a new Flinks Connect."""

    def setUp(self):
        self.client = APIClient()
        self.login_id = str(uuid4())
        self.customer_user = User.objects.create_user(
            email='gad-repull@example.com',
            password='password123',
            full_name='GAD Customer',
            user_type='customer',
        )
        self.staff = User.objects.create_user(
            email='gad-staff@example.com',
            password='password123',
            full_name='GAD Staff',
            user_type='staff',
            is_staff=True,
        )
        self.customer = Customer.objects.create(
            portal_user=self.customer_user,
            first_name='GAD',
            last_name='Customer',
            email='gad-repull@example.com',
            phone='4165550299',
            phone_normalized='4165550299',
            province='ON',
            status='pending',
            onboarding_stage='banking_verification',
            banking_verified=False,
        )
        self.connection = BankConnection.objects.create(
            customer=self.customer,
            login_id=self.login_id,
            provider='flinks',
            is_active=True,
            sync_status='failed',
            sync_error='Read timed out',
        )

    def test_staff_repull_queues_existing_login(self):
        self.client.force_authenticate(user=self.staff)
        with patch('banking.tasks.fetch_flinks_accounts_only.delay') as mocked:
            response = self.client.post(
                f'/api/bank-connections/{self.connection.id}/repull/',
                {},
                format='json',
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], 'SYNCING')
        mocked.assert_called_once_with(str(self.connection.id))

        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_status, 'pending')
        self.assertIsNone(self.connection.sync_error)
        self.assertTrue(
            ActivityHistory.objects.filter(
                customer=self.customer,
                title='IBV Re-pull Started',
            ).exists()
        )

    def test_staff_repull_rejected_when_already_verified(self):
        self.customer.banking_verified = True
        self.customer.save(update_fields=['banking_verified', 'updated_at'])
        self.customer_user.flinks_email = 'already-have@example.com'
        self.customer_user.flinks_name = 'GAD Customer'
        self.customer_user.save(update_fields=['flinks_email', 'flinks_name', 'updated_at'])
        self.connection.sync_status = 'synced'
        self.connection.sync_error = None
        self.connection.save(update_fields=['sync_status', 'sync_error', 'updated_at'])

        self.client.force_authenticate(user=self.staff)
        with patch('banking.tasks.fetch_flinks_accounts_only.delay') as mocked:
            response = self.client.post(
                f'/api/bank-connections/{self.connection.id}/repull/',
                {},
                format='json',
            )
        self.assertEqual(response.status_code, 400)
        mocked.assert_not_called()

    def test_staff_repull_allowed_when_verified_missing_flinks_identity(self):
        self.customer.banking_verified = True
        self.customer.save(update_fields=['banking_verified', 'updated_at'])
        self.connection.sync_status = 'synced'
        self.connection.sync_error = None
        self.connection.save(update_fields=['sync_status', 'sync_error', 'updated_at'])

        self.client.force_authenticate(user=self.staff)
        with patch('banking.tasks.fetch_flinks_accounts_only.delay') as mocked:
            response = self.client.post(
                f'/api/bank-connections/{self.connection.id}/repull/',
                {},
                format='json',
            )
        self.assertEqual(response.status_code, 200, response.data)
        mocked.assert_called_once_with(str(self.connection.id))

    def test_portal_retry_sync_queues_gad(self):
        self.client.force_authenticate(user=self.customer_user)
        with patch('banking.tasks.fetch_flinks_accounts_only.delay') as mocked:
            response = self.client.post('/api/banking/retry-sync/', {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], 'SYNCING')
        mocked.assert_called_once_with(str(self.connection.id))
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_status, 'pending')

    def test_portal_retry_sync_requires_active_flinks_connection(self):
        self.connection.is_active = False
        self.connection.save(update_fields=['is_active', 'updated_at'])
        self.client.force_authenticate(user=self.customer_user)
        with patch('banking.tasks.fetch_flinks_accounts_only.delay') as mocked:
            response = self.client.post('/api/banking/retry-sync/', {}, format='json')
        self.assertEqual(response.status_code, 400)
        mocked.assert_not_called()

    def test_manual_connection_cannot_repull_gad(self):
        self.connection.provider = 'manual'
        self.connection.login_id = f'manual-{self.customer.id}'
        self.connection.save(update_fields=['provider', 'login_id', 'updated_at'])
        self.client.force_authenticate(user=self.staff)
        with patch('banking.tasks.fetch_flinks_accounts_only.delay') as mocked:
            response = self.client.post(
                f'/api/bank-connections/{self.connection.id}/repull/',
                {},
                format='json',
            )
        self.assertEqual(response.status_code, 400)
        mocked.assert_not_called()

    def test_staff_bank_accounts_include_loan_referenced_inactive_connection(self):
        from loans.models import Loan

        self.connection.is_active = False
        self.connection.save(update_fields=['is_active', 'updated_at'])
        account = BankAccount.objects.create(
            connection=self.connection,
            customer=self.customer,
            external_id='inactive-acct',
            name='Home',
            type='checking',
            institution_number='828',
            transit_number='30052',
            account_number='8282',
            is_primary=True,
        )
        Loan.objects.create(
            customer=self.customer,
            principal=Decimal('300.00'),
            fee=Decimal('60.00'),
            total_amount=Decimal('360.00'),
            balance=Decimal('360.00'),
            status='ibv_pending',
            bank_account=account,
            collections_account=account,
            is_active=True,
        )

        self.client.force_authenticate(user=self.staff)
        response = self.client.get(f'/api/bank-accounts/?customer_id={self.customer.id}')
        self.assertEqual(response.status_code, 200)
        payload = response.data if isinstance(response.data, list) else response.data.get('results', [])
        ids = {str(row['id']) for row in payload}
        self.assertIn(str(account.id), ids)

    def test_staff_bank_accounts_hide_inactive_unreferenced_account(self):
        self.connection.is_active = False
        self.connection.save(update_fields=['is_active', 'updated_at'])
        account = BankAccount.objects.create(
            connection=self.connection,
            customer=self.customer,
            external_id='orphan-acct',
            name='Old',
            type='checking',
            institution_number='003',
            transit_number='11111',
            account_number='1111',
        )

        self.client.force_authenticate(user=self.staff)
        response = self.client.get(f'/api/bank-accounts/?customer_id={self.customer.id}')
        self.assertEqual(response.status_code, 200)
        payload = response.data if isinstance(response.data, list) else response.data.get('results', [])
        ids = {str(row['id']) for row in payload}
        self.assertNotIn(str(account.id), ids)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class RepullPendingIbvCommandTests(TestCase):
    """Ops command queues stuck Flinks IBV, plus verified files missing IBV identity."""

    def setUp(self):
        from loans.models import Loan

        self.user = User.objects.create_user(
            email='stuck-ibv@example.com',
            password='password123',
            full_name='Stuck IBV',
            user_type='customer',
        )
        self.customer = Customer.objects.create(
            portal_user=self.user,
            first_name='Stuck',
            last_name='IBV',
            email='stuck-ibv@example.com',
            phone='4165550399',
            phone_normalized='4165550399',
            province='ON',
            status='pending',
            onboarding_stage='banking_verification',
            banking_verified=False,
        )
        self.connection = BankConnection.objects.create(
            customer=self.customer,
            login_id=str(uuid4()),
            provider='flinks',
            is_active=True,
            sync_status='failed',
            sync_error='Read timed out',
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal('300.00'),
            fee=Decimal('60.00'),
            total_amount=Decimal('360.00'),
            balance=Decimal('360.00'),
            status='ibv_pending',
            is_active=True,
        )

    def test_dry_run_does_not_queue(self):
        out = StringIO()
        with patch('banking.tasks.fetch_flinks_accounts_only.delay') as mocked:
            call_command('repull_pending_ibv', stdout=out)
        mocked.assert_not_called()
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_status, 'failed')
        self.assertIn('DRY-RUN: 1 pending IBV', out.getvalue())
        self.assertIn('WOULD REPULL', out.getvalue())

    def test_apply_queues_failed_pending_ibv(self):
        out = StringIO()
        with patch('banking.tasks.fetch_flinks_accounts_only.delay') as mocked:
            call_command('repull_pending_ibv', apply=True, stdout=out)
        mocked.assert_called_once_with(str(self.connection.id))
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_status, 'pending')
        self.assertTrue(
            ActivityHistory.objects.filter(
                customer=self.customer,
                title='IBV Re-pull Started',
            ).exists()
        )
        self.assertIn('Queued 1 IBV re-pull', out.getvalue())

    def test_inline_runs_task_without_celery_delay(self):
        out = StringIO()
        with patch('banking.tasks.fetch_flinks_accounts_only.delay') as delayed, patch(
            'banking.tasks.fetch_flinks_accounts_only.apply'
        ) as applied:
            call_command('repull_pending_ibv', inline=True, stdout=out)
        delayed.assert_not_called()
        applied.assert_called_once_with(args=[str(self.connection.id)])
        self.assertIn('Ran 1 IBV re-pull', out.getvalue())

    def test_skips_verified_synced_connection(self):
        self.customer.banking_verified = True
        self.customer.save(update_fields=['banking_verified', 'updated_at'])
        self.user.flinks_email = 'already-have@example.com'
        self.user.flinks_name = 'Stuck IBV'
        self.user.save(update_fields=['flinks_email', 'flinks_name', 'updated_at'])
        self.connection.sync_status = 'synced'
        self.connection.sync_error = None
        self.connection.save(update_fields=['sync_status', 'sync_error', 'updated_at'])
        self.loan.status = 'pending_signature'
        self.loan.save(update_fields=['status', 'updated_at'])

        with patch('banking.tasks.fetch_flinks_accounts_only.delay') as mocked:
            call_command('repull_pending_ibv', apply=True, stdout=StringIO())
        mocked.assert_not_called()

    def test_includes_verified_synced_missing_flinks_identity(self):
        self.customer.banking_verified = True
        self.customer.save(update_fields=['banking_verified', 'updated_at'])
        self.connection.sync_status = 'synced'
        self.connection.sync_error = None
        self.connection.save(update_fields=['sync_status', 'sync_error', 'updated_at'])
        self.loan.status = 'pending_signature'
        self.loan.save(update_fields=['status', 'updated_at'])

        with patch('banking.tasks.fetch_flinks_accounts_only.delay') as mocked:
            call_command('repull_pending_ibv', apply=True, stdout=StringIO())
        mocked.assert_called_once_with(str(self.connection.id))

    def test_skips_manual_connection(self):
        self.connection.provider = 'manual'
        self.connection.login_id = f'manual-{self.customer.id}'
        self.connection.save(update_fields=['provider', 'login_id', 'updated_at'])
        with patch('banking.tasks.fetch_flinks_accounts_only.delay') as mocked:
            call_command('repull_pending_ibv', apply=True, stdout=StringIO())
        mocked.assert_not_called()

    def test_picks_latest_connection_per_customer(self):
        older = BankConnection.objects.create(
            customer=self.customer,
            login_id=str(uuid4()),
            provider='flinks',
            is_active=False,
            sync_status='failed',
        )
        BankConnection.objects.filter(id=older.id).update(
            created_at=timezone.now() - timedelta(hours=2)
        )

        with patch('banking.tasks.fetch_flinks_accounts_only.delay') as mocked:
            call_command('repull_pending_ibv', apply=True, stdout=StringIO())
        mocked.assert_called_once_with(str(self.connection.id))

    def test_includes_inactive_connection_after_new_application(self):
        self.connection.is_active = False
        self.connection.save(update_fields=['is_active', 'updated_at'])
        with patch('banking.tasks.fetch_flinks_accounts_only.delay') as mocked:
            call_command('repull_pending_ibv', apply=True, stdout=StringIO())
        mocked.assert_called_once_with(str(self.connection.id))
        self.connection.refresh_from_db()
        self.assertTrue(self.connection.is_active)


def _flinks_json_response(status_code, payload):
    return type(
        'Resp',
        (),
        {
            'status_code': status_code,
            'text': str(payload),
            'json': lambda self, p=payload: p,
        },
    )()


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class FlinksGadAutoRepullTests(TestCase):
    """Timeout / 202 / CARD_IN_USE leave IBV pending; beat runs the staff GAD pull."""

    def setUp(self):
        from loans.models import Loan

        self.login_id = str(uuid4())
        self.user = User.objects.create_user(
            email='auto-repull@example.com',
            password='password123',
            full_name='Auto Repull',
            user_type='customer',
        )
        self.customer = Customer.objects.create(
            portal_user=self.user,
            first_name='Auto',
            last_name='Repull',
            email='auto-repull@example.com',
            phone='4165550499',
            phone_normalized='4165550499',
            province='ON',
            status='pending',
            onboarding_stage='banking_verification',
            banking_verified=False,
        )
        self.connection = BankConnection.objects.create(
            customer=self.customer,
            login_id=self.login_id,
            provider='flinks',
            is_active=True,
            sync_status='pending',
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal('300.00'),
            fee=Decimal('60.00'),
            total_amount=Decimal('360.00'),
            balance=Decimal('360.00'),
            status='ibv_pending',
            is_active=True,
        )

    def _age_connection(self, minutes=3):
        BankConnection.objects.filter(id=self.connection.id).update(
            updated_at=timezone.now() - timedelta(minutes=minutes),
        )
        self.connection.refresh_from_db()

    def test_card_in_use_stays_pending_without_broker_repull(self):
        with patch('banking.tasks.fetch_flinks_accounts_only.apply_async') as scheduled, patch(
            'banking.tasks.fetch_flinks_accounts_only.delay'
        ) as delayed, patch(
            'banking.tasks.requests.post',
            return_value=_flinks_json_response(
                400,
                {
                    'HttpStatusCode': 400,
                    'FlinksCode': 'CARD_IN_USE',
                    'Message': 'Call still processing',
                },
            ),
        ):
            result = tasks.fetch_flinks_accounts_only(str(self.connection.id))

        self.assertFalse(result)
        scheduled.assert_not_called()
        delayed.assert_not_called()
        self.connection.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(self.connection.sync_status, 'pending')
        self.assertEqual(self.connection.attempted_syncs, 1)
        self.assertIsNone(self.connection.last_synced_at)
        self.assertIn('awaiting GetAccountsDetail webhook', self.connection.sync_error)
        self.assertFalse(self.customer.banking_verified)
        self.assertEqual(len(mail.outbox), 0)

    def test_operation_pending_async_timeout_stays_pending(self):
        auth = _flinks_json_response(200, {'RequestId': 'req-pending'})
        pending = _flinks_json_response(
            202,
            {
                'HttpStatusCode': 202,
                'FlinksCode': 'OPERATION_PENDING',
                'Message': 'Your operation is still processing.',
            },
        )
        with patch('banking.tasks.FLINKS_ASYNC_MAX_WAIT_SECONDS', 0), patch(
            'banking.tasks.fetch_flinks_accounts_only.apply_async'
        ) as scheduled, patch(
            'banking.tasks._try_cached_flinks_detail',
            return_value=False,
        ), patch(
            'banking.tasks.requests.post',
            side_effect=[auth, pending],
        ), patch(
            'banking.tasks.requests.get',
            return_value=pending,
        ):
            result = tasks.fetch_flinks_accounts_only(str(self.connection.id))

        self.assertFalse(result)
        scheduled.assert_not_called()
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_status, 'pending')
        self.assertIn('awaiting GetAccountsDetail webhook', self.connection.sync_error)
        self.assertFalse(
            ActivityHistory.objects.filter(
                customer=self.customer,
                title='Banking Verification Failed',
            ).exists()
        )

    def test_invalid_login_still_marks_failed(self):
        with patch('banking.tasks.fetch_flinks_accounts_only.apply_async') as scheduled, patch(
            'banking.tasks.requests.post',
            return_value=_flinks_json_response(
                401,
                {
                    'HttpStatusCode': 401,
                    'FlinksCode': 'INVALID_LOGIN',
                    'Message': 'The loginId provided is invalid',
                },
            ),
        ):
            result = tasks.fetch_flinks_accounts_only(str(self.connection.id))

        self.assertFalse(result)
        scheduled.assert_not_called()
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_status, 'failed')
        self.assertFalse(self.customer.banking_verified)
        self.assertTrue(
            ActivityHistory.objects.filter(
                customer=self.customer,
                title='Banking Verification Failed',
            ).exists()
        )

    def test_timeout_does_not_queue_broker_repull(self):
        self.connection.attempted_syncs = 5
        self.connection.save(update_fields=['attempted_syncs', 'updated_at'])

        with patch('banking.tasks.fetch_flinks_accounts_only.apply_async') as scheduled, patch(
            'banking.tasks.fetch_flinks_accounts_only.delay'
        ) as delayed, patch(
            'banking.tasks.requests.post',
            side_effect=requests.exceptions.ReadTimeout('Read timed out'),
        ):
            result = tasks.fetch_flinks_accounts_only(str(self.connection.id))

        self.assertFalse(result)
        scheduled.assert_not_called()
        delayed.assert_not_called()
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.attempted_syncs, 6)
        self.assertEqual(self.connection.sync_status, 'pending')
        self.assertEqual(len(mail.outbox), 0)

    def test_zero_transactions_stays_pending_instead_of_failing(self):
        empty_payload = {
            'Accounts': [
                {
                    'Id': 'acct-empty',
                    'Title': 'KOHO',
                    'Type': 'Chequing',
                    'Currency': 'CAD',
                    'InstitutionNumber': '003',
                    'TransitNumber': '11111',
                    'AccountNumber': '2222',
                    'Transactions': [],
                }
            ]
        }
        with patch('banking.tasks.time.sleep'), patch(
            'banking.tasks.fetch_flinks_accounts_only.apply_async'
        ) as scheduled, patch(
            'banking.tasks.requests.post',
            side_effect=[
                _flinks_json_response(200, {'RequestId': 'req-1'}),
                _flinks_json_response(200, empty_payload),
                _flinks_json_response(200, {'RequestId': 'req-2'}),
                _flinks_json_response(200, empty_payload),
                _flinks_json_response(200, {'RequestId': 'req-3'}),
                _flinks_json_response(200, empty_payload),
                _flinks_json_response(200, {'RequestId': 'req-4'}),
                _flinks_json_response(200, empty_payload),
            ],
        ):
            result = tasks.fetch_flinks_accounts_only(str(self.connection.id))

        self.assertFalse(result)
        scheduled.assert_not_called()
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_status, 'pending')
        self.assertIn('awaiting GetAccountsDetail webhook', self.connection.sync_error)
        self.assertEqual(len(mail.outbox), 0)

    def test_empty_accounts_stays_pending_instead_of_failing(self):
        with patch('banking.tasks.fetch_flinks_accounts_only.apply_async') as scheduled, patch(
            'banking.tasks.requests.post',
            side_effect=[
                _flinks_json_response(200, {'RequestId': 'req-empty'}),
                _flinks_json_response(200, {'Accounts': []}),
            ],
        ):
            result = tasks.fetch_flinks_accounts_only(str(self.connection.id))

        self.assertFalse(result)
        scheduled.assert_not_called()
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_status, 'pending')
        self.assertEqual(len(mail.outbox), 0)

    def test_safety_net_runs_gad_inline_for_pending_ibv(self):
        self.connection.attempted_syncs = 1
        self.connection.sync_error = 'Flinks pull timed out; awaiting GetAccountsDetail webhook.'
        self.connection.save(update_fields=['attempted_syncs', 'sync_error', 'updated_at'])
        self._age_connection()

        with patch('banking.tasks.fetch_flinks_accounts_only.delay') as delayed, patch(
            'banking.tasks.fetch_flinks_accounts_only.apply'
        ) as applied:
            result = tasks.repull_recent_unsynced_ibv()

        self.assertEqual(result['queued'], 1)
        delayed.assert_not_called()
        applied.assert_called_once_with(args=[str(self.connection.id)])
        self.assertTrue(
            ActivityHistory.objects.filter(
                customer=self.customer,
                title='IBV Re-pull Started',
                created_by='system',
            ).exists()
        )

    def test_safety_net_skips_fresh_updated_row(self):
        self.connection.attempted_syncs = 1
        self.connection.save(update_fields=['attempted_syncs', 'updated_at'])

        with patch('banking.tasks.fetch_flinks_accounts_only.apply') as applied:
            result = tasks.repull_recent_unsynced_ibv()

        self.assertEqual(result['queued'], 0)
        applied.assert_not_called()

    def test_safety_net_skips_mfa_challenge(self):
        self.connection.attempted_syncs = 1
        self.connection.sync_error = (
            'Flinks pull timed out; awaiting GetAccountsDetail webhook. '
            '({"HttpStatusCode":203,"SecurityChallenges":[{"Type":"SMS"}]})'
        )
        self.connection.save(update_fields=['attempted_syncs', 'sync_error', 'updated_at'])
        self._age_connection()

        self.assertEqual(tasks.unsynced_ibv_safety_net_targets(), [])
        with patch('banking.tasks.fetch_flinks_accounts_only.apply') as applied:
            result = tasks.repull_recent_unsynced_ibv()
        self.assertEqual(result['queued'], 0)
        applied.assert_not_called()

    def test_safety_net_skips_verified_synced(self):
        self.customer.banking_verified = True
        self.customer.save(update_fields=['banking_verified', 'updated_at'])
        self.connection.sync_status = 'synced'
        self.connection.last_synced_at = timezone.now()
        self.connection.save(update_fields=['sync_status', 'last_synced_at', 'updated_at'])

        self.assertEqual(tasks.unsynced_ibv_safety_net_targets(), [])

    def test_portal_status_exposes_attempted_syncs(self):
        self.connection.attempted_syncs = 2
        self.connection.save(update_fields=['attempted_syncs', 'updated_at'])
        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.get('/api/portal/me/banking/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['attempted_syncs'], 2)

    def test_safety_net_restarts_login_id_older_than_thirty_minutes(self):
        self.connection.attempted_syncs = 1
        self.connection.sync_error = 'Flinks pull timed out; awaiting GetAccountsDetail webhook.'
        self.connection.save(update_fields=['attempted_syncs', 'sync_error', 'updated_at'])
        BankConnection.objects.filter(id=self.connection.id).update(
            created_at=timezone.now() - timedelta(hours=6),
            updated_at=timezone.now() - timedelta(minutes=3),
        )
        self.connection.refresh_from_db()

        with patch('banking.tasks.fetch_flinks_accounts_only.apply') as applied:
            result = tasks.repull_recent_unsynced_ibv()

        self.assertEqual(result['queued'], 1)
        applied.assert_called_once_with(args=[str(self.connection.id)])

    def test_safety_net_restarts_after_several_failed_pulls(self):
        self.connection.attempted_syncs = 5
        self.connection.sync_status = 'failed'
        self.connection.sync_error = 'Failed to fetch accounts from Flinks'
        self.connection.save(
            update_fields=['attempted_syncs', 'sync_status', 'sync_error', 'updated_at']
        )
        self._age_connection()

        self.assertTrue(tasks.should_schedule_gad_repull(self.connection))
        with patch('banking.tasks.fetch_flinks_accounts_only.apply') as applied:
            result = tasks.repull_recent_unsynced_ibv()
        self.assertEqual(result['queued'], 1)
        applied.assert_called_once_with(args=[str(self.connection.id)])

    def test_empty_login_id_cannot_be_repulled(self):
        self.connection.login_id = ''
        self.connection.save(update_fields=['login_id', 'updated_at'])
        self.assertFalse(tasks.should_schedule_gad_repull(self.connection))
        self._age_connection()
        with patch('banking.tasks.fetch_flinks_accounts_only.apply') as applied:
            result = tasks.repull_recent_unsynced_ibv()
        self.assertEqual(result['queued'], 0)
        applied.assert_not_called()

    def test_safety_net_skips_invalid_login(self):
        self.connection.attempted_syncs = 1
        self.connection.sync_status = 'failed'
        self.connection.sync_error = '{"FlinksCode":"INVALID_LOGIN"}'
        self.connection.save(
            update_fields=['attempted_syncs', 'sync_status', 'sync_error', 'updated_at']
        )
        self._age_connection()
        self.assertEqual(tasks.unsynced_ibv_safety_net_targets(), [])

    def test_arrive_customer_is_included_in_automated_repull(self):
        self.customer.source = 'arrive'
        self.customer.arrive_application_id = 'arr-app-1'
        self.customer.save(
            update_fields=['source', 'arrive_application_id', 'updated_at']
        )
        self.connection.attempted_syncs = 1
        self.connection.sync_error = 'Flinks pull timed out; awaiting GetAccountsDetail webhook.'
        self.connection.save(update_fields=['attempted_syncs', 'sync_error', 'updated_at'])
        self._age_connection()

        with patch('banking.tasks.fetch_flinks_accounts_only.apply') as applied:
            result = tasks.repull_recent_unsynced_ibv()

        self.assertEqual(result['queued'], 1)
        applied.assert_called_once_with(args=[str(self.connection.id)])

    def test_pending_ibv_loan_is_a_repull_target(self):
        self.connection.sync_status = 'failed'
        self.connection.sync_error = 'Read timed out'
        self.connection.is_active = False
        self.connection.save(
            update_fields=['sync_status', 'sync_error', 'is_active', 'updated_at']
        )
        ids = {str(row.id) for row in tasks.pending_ibv_repull_targets()}
        self.assertIn(str(self.connection.id), ids)

    def test_safety_net_includes_ibv_pending_inactive_connection(self):
        self.connection.is_active = False
        self.connection.sync_status = 'failed'
        self.connection.sync_error = 'Read timed out'
        self.connection.attempted_syncs = 1
        self.connection.save(
            update_fields=[
                'is_active',
                'sync_status',
                'sync_error',
                'attempted_syncs',
                'updated_at',
            ]
        )
        self._age_connection()

        with patch('banking.tasks.fetch_flinks_accounts_only.apply') as applied:
            result = tasks.repull_recent_unsynced_ibv()

        self.assertEqual(result['queued'], 1)
        applied.assert_called_once_with(args=[str(self.connection.id)])
        self.connection.refresh_from_db()
        self.assertTrue(self.connection.is_active)
        self.assertEqual(self.connection.sync_status, 'pending')
        self.assertIsNone(self.connection.sync_error)
        activity = ActivityHistory.objects.get(
            customer=self.customer,
            title='IBV Re-pull Started',
        )
        self.assertEqual(activity.created_by, 'system')
        self.assertTrue(activity.metadata.get('automated'))
        self.assertTrue(activity.metadata.get('inline'))
        self.assertEqual(activity.metadata.get('trigger'), 'pending_ibv')

    def test_safety_net_skips_live_syncing_but_retries_stale_syncing(self):
        self.connection.sync_status = 'syncing'
        self.connection.attempted_syncs = 1
        self.connection.save(update_fields=['sync_status', 'attempted_syncs', 'updated_at'])
        self._age_connection(minutes=3)
        self.assertEqual(tasks.unsynced_ibv_safety_net_targets(), [])

        BankConnection.objects.filter(id=self.connection.id).update(
            updated_at=timezone.now() - timedelta(
                seconds=tasks.FLINKS_GAD_STALE_SYNCING_SECONDS + 5
            ),
        )
        self.connection.refresh_from_db()
        targets = tasks.unsynced_ibv_safety_net_targets()
        self.assertEqual([str(row.id) for row in targets], [str(self.connection.id)])

    def test_staff_repull_still_uses_delay_auto_uses_apply(self):
        with patch('banking.tasks.fetch_flinks_accounts_only.delay') as delayed, patch(
            'banking.tasks.fetch_flinks_accounts_only.apply'
        ) as applied:
            tasks.queue_flinks_gad_repull(self.connection, user=self.user)

        delayed.assert_called_once_with(str(self.connection.id))
        applied.assert_not_called()

        self.connection.sync_status = 'failed'
        self.connection.save(update_fields=['sync_status', 'updated_at'])
        with patch('banking.tasks.fetch_flinks_accounts_only.delay') as delayed, patch(
            'banking.tasks.fetch_flinks_accounts_only.apply'
        ) as applied:
            tasks.queue_flinks_gad_repull(
                self.connection, trigger='pending_ibv', inline=True
            )

        delayed.assert_not_called()
        applied.assert_called_once_with(args=[str(self.connection.id)])
