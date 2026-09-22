from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import AuthOTPChallenge, Customer, Lender, User
from activity.models import ActivityHistory
from loans.models import Loan, FundedPayment, Payment


class CustomerContactTests(APITestCase):
    def setUp(self):
        self.lender = Lender.default()
        self.staff = User.objects.create_user(
            email='contact-agent@example.test', password='test-password',
            full_name='Contact Agent', user_type='staff', permission_level=2, lender=self.lender,
        )
        self.portal = User.objects.create_user(
            email='original@example.test', password='original-password', full_name='Original Customer',
            phone='+14165550101', phone_normalized='+14165550101', user_type='customer',
            lender=self.lender, flinks_email='verified@example.test', flinks_phone='+14165550102',
        )
        self.customer = Customer.objects.create(
            lender=self.lender, portal_user=self.portal, first_name='Original', last_name='Customer',
            email=self.portal.email, phone=self.portal.phone, phone_normalized=self.portal.phone_normalized,
            phone_verified=True, phone_verified_at=timezone.now(), sms_opted_out=True,
            banking_verified=True, contract_completed=True,
        )
        self.url = f'/api/customers/{self.customer.pk}/contact/'
        self.client.force_authenticate(self.staff)

    def test_saves_contact_and_portal_identity_without_changing_financial_or_verified_data(self):
        loan = Loan.objects.create(customer=self.customer, principal=100, fee=20,
                                   total_amount=120, balance=120, status='active')
        funding = FundedPayment.objects.create(loan=loan, amount=100, status='completed')
        payment = Payment.objects.create(loan=loan, amount=120, scheduled_date=timezone.localdate())
        before = (Loan.objects.get(pk=loan.pk).__dict__.copy(),
                  FundedPayment.objects.get(pk=funding.pk).__dict__.copy(),
                  Payment.objects.get(pk=payment.pk).__dict__.copy())
        with patch('accounts.tasks.send_email_otp_task.delay') as email_task, \
             patch('accounts.tasks.send_sms_otp_task.delay') as sms_task:
            response = self.client.patch(self.url, {'email': 'Updated@Example.test', 'phone': '(416) 555-0110'})
        self.assertEqual(response.status_code, 200, response.data)
        email_task.assert_not_called()
        sms_task.assert_not_called()
        self.customer.refresh_from_db()
        self.portal.refresh_from_db()
        self.assertEqual(self.customer.email, 'updated@example.test')
        self.assertEqual(self.portal.email, self.customer.email)
        self.assertEqual(self.customer.phone, '+14165550110')
        self.assertEqual(self.portal.phone_normalized, self.customer.phone_normalized)
        self.assertFalse(self.customer.phone_verified)
        self.assertIsNone(self.customer.phone_verified_at)
        self.assertTrue(self.customer.sms_opted_out)
        self.assertTrue(self.customer.banking_verified)
        self.assertTrue(self.customer.contract_completed)
        self.assertEqual(self.portal.flinks_email, 'verified@example.test')
        self.assertEqual(self.portal.flinks_phone, '+14165550102')
        self.assertTrue(self.portal.check_password('original-password'))
        for saved, model, pk in zip(before, (Loan, FundedPayment, Payment), (loan.pk, funding.pk, payment.pk)):
            saved.pop('_state')
            after = model.objects.get(pk=pk).__dict__.copy()
            after.pop('_state')
            self.assertEqual(saved, after)
        audit = ActivityHistory.objects.get(customer=self.customer, type='customer_updated')
        self.assertEqual(audit.created_by, str(self.staff.pk))
        self.assertEqual(set(audit.metadata['changes']), {'email', 'phone'})

    def test_partial_email_edit_preserves_phone_verification(self):
        response = self.client.patch(self.url, {'email': 'new-email@example.test'})
        self.assertEqual(response.status_code, 200, response.data)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.phone_verified)
        self.assertEqual(self.customer.phone, '+14165550101')

    def test_invalid_and_duplicate_contact_changes_are_atomic(self):
        other = User.objects.create_user(email='taken@example.test', full_name='Other',
                                         phone_normalized='+14165550199', user_type='customer')
        Customer.objects.create(first_name='Other', last_name='Customer', email=other.email,
                                phone=other.phone_normalized, phone_normalized=other.phone_normalized)
        for payload in (
            {'email': 'bad-email'}, {'phone': '123'}, {'email': ''}, {'phone': ''},
            {'email': 'TAKEN@example.test', 'phone': '+14165550110'},
            {'email': 'free@example.test', 'phone': '(416)555-0199'},
            {'email': 'free@example.test', 'flinks_email': 'changed@example.test'}, {},
        ):
            with self.subTest(payload=payload):
                response = self.client.patch(self.url, payload)
                self.assertEqual(response.status_code, 400, response.data)
                self.customer.refresh_from_db()
                self.portal.refresh_from_db()
                self.assertEqual(self.customer.email, 'original@example.test')
                self.assertEqual(self.portal.email, 'original@example.test')
                self.assertEqual(self.customer.phone_normalized, '+14165550101')
        self.assertFalse(ActivityHistory.objects.filter(type='customer_updated').exists())

    def test_contact_requires_agent_and_same_lender(self):
        self.staff.permission_level = 1
        self.staff.save(update_fields=['permission_level'])
        self.assertEqual(self.client.patch(self.url, {'phone': '4165550110'}).status_code, 403)
        self.client.force_authenticate(self.portal)
        self.assertEqual(self.client.patch(self.url, {'phone': '4165550110'}).status_code, 403)
        self.staff.permission_level = 2
        self.staff.lender = Lender.objects.create(name='Other', slug='contact-other')
        self.staff.save(update_fields=['permission_level', 'lender'])
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.patch(self.url, {'phone': '4165550110'}).status_code, 404)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.patch(self.url, {'phone': '4165550110'}).status_code, 401)

    def test_contact_without_portal_user_and_noop(self):
        self.customer.portal_user = None
        self.customer.save(update_fields=['portal_user'])
        response = self.client.patch(self.url, {'email': 'standalone@example.test'})
        self.assertEqual(response.status_code, 200, response.data)
        response = self.client.patch(self.url, {'email': 'standalone@example.test'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(ActivityHistory.objects.filter(type='customer_updated').count(), 1)

    def test_pending_and_verified_codes_are_expired_for_updated_login_only(self):
        challenges = [AuthOTPChallenge.objects.create(
            purpose=purpose, identifier=self.portal.email, status=status, code_hash='test',
            expires_at=timezone.now() + timedelta(minutes=10), metadata={'user_id': str(self.portal.pk)},
        ) for purpose, status in (
            (AuthOTPChallenge.PURPOSE_LOGIN_EMAIL, AuthOTPChallenge.STATUS_PENDING),
            (AuthOTPChallenge.PURPOSE_PASSWORD_RESET_EMAIL, AuthOTPChallenge.STATUS_VERIFIED),
        )]
        response = self.client.patch(self.url, {'email': 'updated@example.test'})
        self.assertEqual(response.status_code, 200, response.data)
        for challenge in challenges:
            challenge.refresh_from_db()
            self.assertEqual(challenge.status, AuthOTPChallenge.STATUS_EXPIRED)
