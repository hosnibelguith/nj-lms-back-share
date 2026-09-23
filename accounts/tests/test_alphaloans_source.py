from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import AuthOTPChallenge, Customer, Lender, User
from loans.models import Loan, Payment


@override_settings(DEBUG=True, DEV_OTP_CODE='123456')
class AlphaloansSourceTests(APITestCase):
    def setUp(self):
        self.lender = Lender.default()
        self.staff = User.objects.create_user(
            email='alpha-staff@example.test', full_name='Source Test', password='Password123!',
            user_type='staff', permission_level=5, lender=self.lender,
        )
        self.payload = {
            'first_name': 'Alpha', 'last_name': 'Applicant', 'email': 'alpha-applicant@example.test',
            'phone': '4165550199', 'province': 'ON', 'date_of_birth': '1990-01-01',
            'requested_loan_amount': '700.00', 'password': 'Password123!', 'confirm_password': 'Password123!',
        }

    def signup(self, source=None, legacy_challenge=False):
        payload = dict(self.payload)
        if source is not None:
            payload['source'] = source
        with patch('accounts.serializers.send_sms_otp_task.delay'):
            response = self.client.post('/api/portal/signup/start/', payload, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        challenge = AuthOTPChallenge.objects.get(pk=response.data['challenge_id'])
        if legacy_challenge:
            challenge.metadata.pop('source', None)
            challenge.save(update_fields=['metadata'])
        else:
            self.assertEqual(challenge.metadata['source'], source or 'organic')
        with patch('accounts.views.send_welcome_email.delay'):
            verified = self.client.post('/api/portal/signup/verify-phone/', {
                'challenge_id': str(challenge.pk), 'code': '123456',
                'source': 'arrive',  # The verification request cannot replace saved attribution.
            }, format='json')
        self.assertEqual(verified.status_code, 201, verified.data)
        return Customer.objects.get(email=payload['email'])

    def test_alphaloans_survives_phone_verification_and_is_visible_to_staff(self):
        customer = self.signup('alphaloans')
        self.assertEqual(customer.source, 'alphaloans')
        self.assertEqual(customer.lender, self.lender)
        self.assertEqual(customer.loans.count(), 1)
        self.assertEqual(customer.loans.get().status, 'ibv_pending')
        self.assertFalse(customer.arrive_application_id)
        self.client.force_authenticate(self.staff)
        detail = self.client.get(f'/api/customers/{customer.pk}/')
        self.assertEqual(detail.data['source'], 'alphaloans')
        loans = self.client.get('/api/loans/', {'source': 'alphaloans'})
        self.assertEqual(loans.status_code, 200, loans.data)
        self.assertEqual(loans.data['count'], 1)
        self.assertEqual(loans.data['results'][0]['customer_source'], 'alphaloans')
        self.assertFalse(loans.data['results'][0]['is_arrive'])

    def test_normal_signup_remains_landing(self):
        self.assertEqual(self.signup().source, 'organic')

    def test_pending_signup_started_before_deployment_still_works(self):
        self.assertEqual(self.signup(legacy_challenge=True).source, 'organic')

    def test_public_signup_cannot_select_arrive_or_unknown_sources(self):
        for source in ('arrive', 'unknown'):
            with self.subTest(source=source), patch('accounts.serializers.send_sms_otp_task.delay') as sms:
                response = self.client.post('/api/portal/signup/start/', {
                    **self.payload, 'source': source,
                }, format='json')
                self.assertEqual(response.status_code, 400, response.data)
                sms.assert_not_called()
        self.assertFalse(Customer.objects.exists())
        self.assertFalse(AuthOTPChallenge.objects.exists())

    def test_existing_account_is_not_retagged_by_an_unauthenticated_link(self):
        customer = self.signup()
        loan_id = customer.loans.get().pk
        self.client.logout()
        response = self.client.post('/api/portal/signup/start/', {
            **self.payload, 'source': 'alphaloans',
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['existing_account'])
        customer.refresh_from_db()
        self.assertEqual(customer.source, 'organic')
        self.assertEqual(list(customer.loans.values_list('pk', flat=True)), [loan_id])

    def test_source_filters_and_dashboard_do_not_mix_alphaloans_with_landing(self):
        for index, source in enumerate(('organic', 'arrive', 'alphaloans')):
            customer = Customer.objects.create(
                lender=self.lender, first_name=source, last_name='Test', source=source,
                email=f'{source}@example.test', phone=f'416555010{index}',
            )
            loan = Loan.objects.create(customer=customer, principal=100, fee=0,
                                       total_amount=100, balance=100, status='active')
            Payment.objects.create(loan=loan, amount=100, scheduled_date=timezone.localdate(),
                                   status='nsf' if source == 'alphaloans' else 'completed',
                                   processed_at=timezone.now())
            if source == 'alphaloans':
                Payment.objects.create(loan=loan, amount=10, scheduled_date=timezone.localdate(),
                                       type='manual', status='completed', processed_at=timezone.now())
        self.client.force_authenticate(self.staff)
        totals = self.client.get('/api/loans/dashboard/analytics/').data['totals']
        self.assertEqual(totals['received_applications_count'], 3)
        for source in ('organic', 'arrive', 'alphaloans'):
            with self.subTest(source=source):
                self.assertEqual(totals[f'received_{source}_count'], 1)
                analytics = self.client.get('/api/loans/dashboard/analytics/', {'source': source}).data
                self.assertEqual(analytics['totals']['received_applications_count'], 1)
                self.assertEqual(analytics['totals']['resolved_payments_count'], 1)
                self.assertEqual(analytics['totals']['nsf_ratio'], 100 if source == 'alphaloans' else 0)
                self.assertEqual(sum(p['value'] for p in analytics['series'][f'received_{source}_count']), 1)
                listing = self.client.get('/api/loans/', {'source': source}).data
                self.assertEqual(listing['count'], 1)
                self.assertEqual(listing['results'][0]['customer_source'], source)
                for report in ('customer', 'loan', 'payment', 'leads'):
                    response = self.client.get('/api/loans/reports/', {'report_type': report, 'source': source})
                    self.assertEqual(response.status_code, 200, response.data)
                    self.assertEqual(response.data['count'], 1)
