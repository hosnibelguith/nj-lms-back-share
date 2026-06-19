from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import Customer, User
from banking.models import BankAccount, BankConnection
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

    def test_institution_621_is_accepted(self):
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
                    'InstitutionNumber': '621',
                    'TransitNumber': '12345',
                    'AccountNumber': '9999999999',
                    'Transactions': [{'Id': 'tx-621', 'Date': '2026-01-01', 'Description': 'Pay', 'Credit': 50}],
                }
            ]
        }

        with patch('banking.tasks.requests.post') as mocked_post:
            mocked_post.side_effect = [
                type('Resp', (), {'status_code': 200, 'json': lambda self: {'RequestId': 'req-621'}, 'text': ''})(),
                type('Resp', (), {'status_code': 200, 'json': lambda self: accounts_payload, 'text': ''})(),
            ]
            self.assertTrue(tasks.fetch_flinks_accounts_only(str(connection.id)))

        connection.refresh_from_db()
        self.customer_a.refresh_from_db()
        self.assertEqual(connection.sync_status, 'synced')
        self.assertTrue(self.customer_a.banking_verified)
