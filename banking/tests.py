from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import Customer, User
from activity.models import ActivityHistory
from banking.models import BankAccount, BankConnection, BankTransaction
from banking import tasks


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
