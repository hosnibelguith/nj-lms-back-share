import json
from datetime import datetime, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import Mock, patch

from django.contrib import admin
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Customer, Lender, User
from loans.models import CollectionPayment, Loan
from .admin import PluginSettingsAdmin
from .client import OceonError
from .models import DashboardSnapshot, PluginSettings, RejectDelivery
from .services import (TORONTO, capture_final_failure, deliver, enqueue, finish_capture,
                       payload_for, reconcile, refresh_dashboard, retry_corrected, source_rejects)


class IntegrationTests(APITestCase):
    def setUp(self):
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('Real HTTP forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.dispatch = patch('icollector.tasks.send_reject.apply_async')
        self.dispatch_mock = self.dispatch.start()
        self.addCleanup(self.dispatch.stop)
        self.lender = Lender.default()
        self.other_lender = Lender.objects.create(name='Other', slug='other')
        self.staff = User.objects.create_user(email='agent@example.test', password='local-only',
            user_type='staff', lender=self.lender)
        self.client.force_authenticate(self.staff)
        self.customer = Customer.objects.create(first_name='Test', last_name='Customer',
            email='customer@example.test', phone='+14165550100', lender=self.lender)
        self.loan = Loan.objects.create(customer=self.customer, principal=Decimal('1000.00'),
            fee=Decimal('50.00'), total_amount=Decimal('1050.00'), balance=Decimal('1050.00'), status='active')
        self.config = PluginSettings.objects.get(pk=1)
        self.config.enabled = True
        self.config.save()
        self.remote = Mock()
        self.remote.send_reject.return_value = {'http_status': 201, 'row_id': 'row-1'}
        self.remote.dashboard.return_value = {'dashboard': 'Main Dashboard', 'widgets': []}

    def rejection(self, **kwargs):
        return CollectionPayment.objects.create(loan=self.loan, amount=Decimal('250.50'),
            status=kwargs.pop('status', 'failed'), failure_reason='Insufficient funds',
            returned_at=kwargs.pop('returned_at', timezone.now()), **kwargs)

    def test_default_migration_is_disabled(self):
        from importlib import import_module
        from django.apps import apps
        PluginSettings.objects.all().delete()
        migration = import_module('icollector.migrations.0002_disabled_switch')
        migration.create_disabled_switch(apps, Mock(connection=Mock(alias='default')))
        self.assertFalse(PluginSettings.objects.get(pk=1).enabled)

    def test_capture_waits_for_commit_and_does_not_call_provider(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            source = self.rejection()
        self.assertEqual(RejectDelivery.objects.count(), 1)
        self.assertEqual(RejectDelivery.objects.get(pk=source.pk).payload, {})
        self.dispatch_mock.assert_not_called()
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(RejectDelivery.objects.get(pk=source.pk).payload['data']['Balance'], 1050.0)
        self.dispatch_mock.assert_called_once()

    def test_rolled_back_failure_does_not_leave_delivery(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.rejection()
                raise RuntimeError('rolled back')
        self.assertEqual(RejectDelivery.objects.count(), 0)

    def test_duplicate_event_uses_one_row_but_two_attempts_are_distinct(self):
        source = self.rejection()
        source.save()
        self.assertFalse(enqueue(source.pk))
        self.rejection()
        self.assertEqual(RejectDelivery.objects.count(), 2)

    def test_other_lender_is_never_captured(self):
        self.customer.lender = self.other_lender
        self.customer.save()
        self.rejection()
        self.assertEqual(RejectDelivery.objects.count(), 0)

    def test_cancelled_and_completed_attempts_are_not_captured(self):
        self.rejection(status='cancelled', zum_status='Failed')
        self.rejection(status='completed')
        self.assertEqual(RejectDelivery.objects.count(), 0)

    def test_processor_failure_is_captured_before_local_status_catches_up(self):
        source = self.rejection(status='processing', zum_status='Failed')
        self.assertTrue(RejectDelivery.objects.filter(pk=source.pk).exists())

    def test_mapping_matches_csv_and_toronto_return_day(self):
        source = self.rejection(returned_at=datetime.fromisoformat('2026-09-15T02:30:00+00:00'))
        payload = payload_for(source)
        self.assertEqual(payload, {'idempotency_key': str(source.pk), 'data': {
            'Client name': 'Test Customer', 'Email': 'customer@example.test', 'Phone': '+14165550100',
            'Reason': 'Insufficient funds', 'Missed payment': 250.5, 'Balance': 1050.0,
            'Returned date': '2026-09-14'}})

    def test_balance_is_frozen_after_failure_adjustments(self):
        source = self.rejection()
        Loan.objects.filter(pk=self.loan.pk).update(balance=Decimal('1350.50'))
        capture_final_failure(source.pk)
        Loan.objects.filter(pk=self.loan.pk).update(balance=Decimal('1000.00'))
        finish_capture(source.pk)
        deliver(source.pk, self.remote)
        self.assertEqual(self.remote.send_reject.call_args.args[0]['data']['Balance'], 1350.5)

    def test_real_settled_return_captures_balance_after_restoration(self):
        from loans.zumrails import apply_collection_failure
        source = self.rejection(status='completed')
        self.assertFalse(RejectDelivery.objects.exists())
        with patch('loans.services.LoanService.apply_collection_failure_fee', return_value=None):
            apply_collection_failure(source, reason='Insufficient funds')
        self.loan.refresh_from_db()
        row = RejectDelivery.objects.get(pk=source.pk)
        self.assertEqual(self.loan.balance, Decimal('1300.50'))
        self.assertEqual(row.payload['data']['Balance'], 1300.5)

    def test_invalid_phone_requires_review_without_repeated_requests(self):
        self.customer.phone = '123'
        self.customer.save()
        source = self.rejection()
        self.assertFalse(deliver(source.pk, self.remote))
        self.assertEqual(RejectDelivery.objects.get(pk=source.pk).status, 'blocked')
        self.remote.send_reject.assert_not_called()

    def test_contact_correction_preserves_frozen_financial_facts(self):
        self.customer.phone = '123'
        self.customer.save()
        source = self.rejection()
        capture_final_failure(source.pk)
        self.assertFalse(deliver(source.pk, self.remote))
        Loan.objects.filter(pk=self.loan.pk).update(balance=Decimal('999.00'))
        self.customer.phone = '+14165550101'
        self.customer.save()
        self.assertTrue(retry_corrected(source.pk))
        self.assertTrue(deliver(source.pk, self.remote))
        sent = self.remote.send_reject.call_args.args[0]['data']
        self.assertEqual(sent['Balance'], 1050.0)
        self.assertEqual(sent['Missed payment'], 250.5)
        self.assertEqual(sent['Phone'], '+14165550101')

    def test_disabled_stops_capture_delivery_and_dashboard(self):
        source = self.rejection()
        self.config.enabled = False
        self.config.save()
        self.rejection()
        self.assertEqual(RejectDelivery.objects.count(), 1)
        self.assertFalse(deliver(source.pk, self.remote))
        self.assertFalse(refresh_dashboard(self.remote))
        self.remote.send_reject.assert_not_called()
        self.remote.dashboard.assert_not_called()
        self.assertEqual(self.client.get('/api/icollector/dashboard/').status_code, 403)

    def test_reenable_recovers_rejects_received_while_paused(self):
        original = self.config.capture_since
        self.config.enabled = False
        self.config.save()
        self.rejection()
        self.config.enabled = True
        self.config.save()
        self.assertEqual(self.config.capture_since, original)
        self.assertEqual(reconcile(), 1)
        self.assertEqual(reconcile(), 0)

    def test_retry_preserves_payload_and_idempotency_key(self):
        source = self.rejection()
        self.remote.send_reject.side_effect = OceonError('Oceon request timed out.')
        self.assertFalse(deliver(source.pk, self.remote))
        row = RejectDelivery.objects.get(pk=source.pk)
        original = row.payload
        self.assertEqual(row.status, 'pending')
        self.assertGreater(row.next_attempt_at, timezone.now())
        Loan.objects.filter(pk=self.loan.pk).update(balance=Decimal('999.00'))
        RejectDelivery.objects.filter(pk=source.pk).update(next_attempt_at=timezone.now())
        self.remote.send_reject.side_effect = None
        self.assertTrue(deliver(source.pk, self.remote))
        self.assertEqual(self.remote.send_reject.call_args.args[0], original)
        self.assertFalse(deliver(source.pk, self.remote))
        self.assertEqual(self.remote.send_reject.call_count, 2)

    def test_active_delivery_lease_prevents_concurrent_send(self):
        source = self.rejection()
        RejectDelivery.objects.filter(pk=source.pk).update(status='sending', lease_until=timezone.now()+timedelta(minutes=1))
        self.assertFalse(deliver(source.pk, self.remote))
        self.remote.send_reject.assert_not_called()

    def test_expired_lease_recovers_interrupted_delivery(self):
        source = self.rejection()
        RejectDelivery.objects.filter(pk=source.pk).update(status='sending', lease_until=timezone.now()-timedelta(seconds=1))
        self.assertTrue(deliver(source.pk, self.remote))

    def test_no_longer_rejected_source_is_not_delivered(self):
        source = self.rejection()
        CollectionPayment.objects.filter(pk=source.pk).update(status='cancelled')
        self.assertFalse(deliver(source.pk, self.remote))
        self.assertEqual(RejectDelivery.objects.get(pk=source.pk).status, 'cancelled')

    def test_broker_failure_leaves_durable_snapshot(self):
        source = self.rejection()
        self.dispatch_mock.side_effect = RuntimeError('broker unavailable')
        finish_capture(source.pk)
        self.assertTrue(RejectDelivery.objects.get(pk=source.pk).payload)

    def test_capture_database_error_does_not_break_payment_save(self):
        with patch('icollector.services.enqueue', side_effect=IntegrityError('temporary')):
            source = self.rejection()
        self.assertTrue(CollectionPayment.objects.filter(pk=source.pk).exists())
        self.assertEqual(reconcile(), 1)

    def test_staff_access_and_cross_lender_denial(self):
        self.assertEqual(self.client.get('/api/icollector/availability/').status_code, 200)
        self.staff.lender = self.other_lender
        self.staff.is_superuser = True
        self.staff.save()
        self.assertEqual(self.client.get('/api/icollector/dashboard/').status_code, 403)

    def test_customer_cannot_read_dashboard_or_toggle_plugin(self):
        self.staff.user_type = 'customer'
        self.staff.save()
        self.assertEqual(self.client.get('/api/icollector/dashboard/').status_code, 403)
        self.assertFalse(PluginSettingsAdmin(PluginSettings, admin.site).has_change_permission(Mock(user=self.staff)))

    def test_dashboard_get_only_reads_cache_and_reports_no_snapshot(self):
        result = self.client.get('/api/icollector/dashboard/')
        self.assertFalse(result.data['available'])
        self.assertIsNone(result.data['dashboard'])
        self.assertEqual(DashboardSnapshot.objects.count(), 0)
        self.assertEqual(result['Cache-Control'], 'no-store, private')

    def test_polling_is_deduplicated_and_retains_last_success(self):
        self.assertTrue(refresh_dashboard(self.remote))
        self.assertFalse(refresh_dashboard(self.remote))
        DashboardSnapshot.objects.update(attempted_at=timezone.now()-timedelta(seconds=31))
        self.remote.dashboard.side_effect = OceonError('Oceon unavailable.', 503)
        self.assertFalse(refresh_dashboard(self.remote))
        result = self.client.get('/api/icollector/dashboard/')
        self.assertTrue(result.data['available'])
        self.assertEqual(result.data['dashboard']['dashboard'], 'Main Dashboard')
        self.assertEqual(result.data['error'], 'Oceon unavailable.')

    def test_backfill_preview_is_read_only_and_apply_requires_fingerprint(self):
        self.config.enabled = False
        self.config.save()
        source = self.rejection(returned_at=datetime.fromisoformat('2026-09-15T02:00:00+00:00'))
        self.rejection(returned_at=datetime.fromisoformat('2026-09-15T05:00:00+00:00'))
        stdout = StringIO()
        call_command('icollector_backfill', date='2026-09-14', stdout=stdout)
        preview = json.loads(stdout.getvalue())
        self.assertEqual(preview['count'], 1)
        self.assertEqual(RejectDelivery.objects.count(), 0)
        self.config.enabled = True
        self.config.save()
        with self.assertRaises(CommandError):
            call_command('icollector_backfill', date='2026-09-14', apply=True, fingerprint='wrong')
        call_command('icollector_backfill', date='2026-09-14', apply=True, fingerprint=preview['fingerprint'], stdout=StringIO())
        self.assertEqual(list(RejectDelivery.objects.values_list('pk', flat=True)), [source.pk])
