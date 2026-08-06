from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

from accounts.models import Customer, User
from accounts.tasks import send_sms_otp_task
from activity.models import ActivityHistory
from communications.models import Communication
from communications.tasks import send_sms
from communications.twilio_sms import (
    TwilioConfigurationError,
    TwilioService,
    classify_inbound_keyword,
    is_opt_out_error,
    map_message_status,
    normalize_e164,
)

TWILIO_SETTINGS = dict(
    TWILIO_ACCOUNT_SID='AC00000000000000000000000000000001',
    TWILIO_AUTH_TOKEN='auth-token-1',
    TWILIO_PHONE_NUMBER='+14165550000',
    TWILIO_MESSAGING_PHONE_NUMBER='',
    TWILIO_MESSAGING_SERVICE_SID='',
    TWILIO_STATUS_CALLBACK_URL='',
)

WEBHOOK_URL = 'http://testserver/api/webhooks/twilio/'


def _twilio_message(sid='SM1'):
    message = Mock()
    message.sid = sid
    return message


class TwilioHelperTests(TestCase):
    def test_normalize_e164_adds_country_code(self):
        self.assertEqual(normalize_e164('(416) 555-1234'), '+14165551234')
        self.assertEqual(normalize_e164('14165551234'), '+14165551234')
        self.assertEqual(normalize_e164('+1 416-555-1234'), '+14165551234')
        self.assertEqual(normalize_e164('123'), '')

    def test_message_status_mapping_covers_documented_values(self):
        self.assertEqual(map_message_status('delivered'), 'delivered')
        self.assertEqual(map_message_status('sent'), 'sent')
        self.assertEqual(map_message_status('queued'), 'pending')
        self.assertEqual(map_message_status('undelivered'), 'failed')
        self.assertEqual(map_message_status('failed'), 'failed')
        self.assertIsNone(map_message_status('something-new'))

    def test_unsubscribe_error_codes_are_recognised(self):
        self.assertTrue(is_opt_out_error('21610'))
        self.assertTrue(is_opt_out_error(21614))
        self.assertFalse(is_opt_out_error('30007'))

    def test_reserved_keywords_are_classified(self):
        for word in ('STOP', 'stop', 'Unsubscribe', 'CANCEL', 'ARRET'):
            self.assertEqual(classify_inbound_keyword(word), 'stop', word)
        for word in ('START', 'yes', 'UNSTOP'):
            self.assertEqual(classify_inbound_keyword(word), 'start', word)
        self.assertIsNone(classify_inbound_keyword('when is my payment due'))


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, **TWILIO_SETTINGS)
class TwilioSendTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(
            first_name='Text',
            last_name='Customer',
            email='sms@example.com',
            phone='4165551234',
            phone_normalized='4165551234',
            province='ON',
            status='pending',
        )

    def _communication(self):
        return Communication.objects.create(
            customer=self.customer,
            type='sms',
            direction='outbound',
            to_phone=self.customer.phone,
            content='Your payment is due tomorrow.',
            status='pending',
        )

    @patch('twilio.rest.Client')
    def test_send_sms_uses_documented_message_payload(self, mock_client):
        mock_client.return_value.messages.create.return_value = _twilio_message('SM123')

        communication = self._communication()
        send_sms(str(communication.id))

        mock_client.assert_called_once_with(
            'AC00000000000000000000000000000001', 'auth-token-1'
        )
        self.assertEqual(
            mock_client.return_value.messages.create.call_args.kwargs,
            {
                'to': '+14165551234',
                'body': 'Your payment is due tomorrow.',
                'from_': '+14165550000',
            },
        )

        communication.refresh_from_db()
        self.assertEqual(communication.status, 'sent')
        self.assertEqual(communication.external_id, 'SM123')
        self.assertIsNotNone(communication.sent_at)

    @override_settings(TWILIO_MESSAGING_SERVICE_SID='MG1')
    @patch('twilio.rest.Client')
    def test_messaging_service_takes_precedence_over_number(self, mock_client):
        mock_client.return_value.messages.create.return_value = _twilio_message()

        send_sms(str(self._communication().id))

        payload = mock_client.return_value.messages.create.call_args.kwargs
        self.assertEqual(payload['messaging_service_sid'], 'MG1')
        self.assertNotIn('from_', payload)

    @override_settings(TWILIO_MESSAGING_PHONE_NUMBER='+1 (438) 807-0978')
    @patch('twilio.rest.Client')
    def test_staff_sms_uses_the_conversational_number(self, mock_client):
        mock_client.return_value.messages.create.return_value = _twilio_message()

        send_sms(str(self._communication().id))

        payload = mock_client.return_value.messages.create.call_args.kwargs
        self.assertEqual(payload['from_'], '+14388070978')

    @override_settings(TWILIO_MESSAGING_PHONE_NUMBER='+14388070978')
    @patch('twilio.rest.Client')
    def test_otp_stays_on_its_own_number_when_texting_moves(self, mock_client):
        mock_client.return_value.messages.create.return_value = _twilio_message()

        send_sms_otp_task('4165551234', '123456')

        payload = mock_client.return_value.messages.create.call_args.kwargs
        self.assertEqual(payload['from_'], '+14165550000')

    @override_settings(TWILIO_MESSAGING_PHONE_NUMBER='')
    @patch('twilio.rest.Client')
    def test_staff_sms_falls_back_to_the_otp_number(self, mock_client):
        mock_client.return_value.messages.create.return_value = _twilio_message()

        send_sms(str(self._communication().id))

        payload = mock_client.return_value.messages.create.call_args.kwargs
        self.assertEqual(payload['from_'], '+14165550000')

    @override_settings(
        TWILIO_MESSAGING_PHONE_NUMBER='+14388070978',
        TWILIO_MESSAGING_SERVICE_SID='MG1',
    )
    @patch('twilio.rest.Client')
    def test_messaging_service_never_carries_verification_codes(self, mock_client):
        mock_client.return_value.messages.create.return_value = _twilio_message()

        send_sms(str(self._communication().id))

        payload = mock_client.return_value.messages.create.call_args.kwargs
        self.assertEqual(payload['messaging_service_sid'], 'MG1')

        send_sms_otp_task('4165551234', '123456')

        payload = mock_client.return_value.messages.create.call_args.kwargs
        self.assertEqual(payload['from_'], '+14165550000')
        self.assertNotIn('messaging_service_sid', payload)

    @override_settings(TWILIO_STATUS_CALLBACK_URL=WEBHOOK_URL)
    @patch('twilio.rest.Client')
    def test_status_callback_is_attached_when_configured(self, mock_client):
        mock_client.return_value.messages.create.return_value = _twilio_message()

        send_sms(str(self._communication().id))

        payload = mock_client.return_value.messages.create.call_args.kwargs
        self.assertEqual(payload['status_callback'], WEBHOOK_URL)

    @patch('twilio.rest.Client')
    def test_provider_rejection_marks_communication_failed(self, mock_client):
        mock_client.return_value.messages.create.side_effect = TwilioRestException(
            status=400, uri='/Messages', msg='Invalid body', code=21602
        )

        communication = self._communication()
        with self.assertRaises(Exception):
            send_sms(str(communication.id))

        communication.refresh_from_db()
        self.assertEqual(communication.status, 'failed')
        self.assertIn('21602', communication.error_message)

    @patch('twilio.rest.Client')
    def test_unsubscribed_recipient_opts_the_customer_out(self, mock_client):
        mock_client.return_value.messages.create.side_effect = TwilioRestException(
            status=400,
            uri='/Messages',
            msg='The message cannot be sent to an unsubscribed recipient',
            code=21610,
        )

        communication = self._communication()
        send_sms(str(communication.id))

        communication.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(communication.status, 'failed')
        self.assertTrue(self.customer.sms_opted_out)
        self.assertIsNotNone(self.customer.sms_opted_out_at)

    @override_settings(TWILIO_ACCOUNT_SID='', TWILIO_AUTH_TOKEN='')
    def test_missing_credentials_fail_fast_without_pretending_to_send(self):
        communication = self._communication()
        send_sms(str(communication.id))

        communication.refresh_from_db()
        self.assertEqual(communication.status, 'failed')
        self.assertIn('not configured', communication.error_message)
        self.assertIsNone(communication.sent_at)

    def test_opted_out_customer_is_never_texted(self):
        self.customer.sms_opted_out = True
        self.customer.save(update_fields=['sms_opted_out'])
        communication = self._communication()

        with patch('twilio.rest.Client') as mock_client:
            send_sms(str(communication.id))
            mock_client.assert_not_called()

        communication.refresh_from_db()
        self.assertEqual(communication.status, 'failed')
        self.assertIn('opted out', communication.error_message)

    def test_invalid_phone_number_is_rejected(self):
        with self.assertRaises(TwilioConfigurationError):
            TwilioService.send_sms(to='123', content='hi')


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, **TWILIO_SETTINGS)
class TwilioWebhookTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = '/api/webhooks/twilio/'
        self.customer = Customer.objects.create(
            first_name='Webhook',
            last_name='Customer',
            email='webhook-sms@example.com',
            phone='4165559999',
            phone_normalized='4165559999',
            province='ON',
            status='pending',
        )
        self.communication = Communication.objects.create(
            customer=self.customer,
            type='sms',
            direction='outbound',
            to_phone='4165559999',
            content='Hello',
            status='sent',
            external_id='SM-outbound-1',
        )

    def _post(self, payload, signature=None):
        if signature is None:
            signature = RequestValidator('auth-token-1').compute_signature(
                WEBHOOK_URL, payload
            )
        return self.client.post(self.url, payload, HTTP_X_TWILIO_SIGNATURE=signature)

    def test_rejects_missing_or_forged_signature(self):
        payload = {'MessageSid': 'SM-outbound-1', 'MessageStatus': 'delivered'}

        self.assertEqual(self.client.post(self.url, payload).status_code, 403)

        response = self._post(payload, signature='forged')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['error'], 'invalid_signature')

        self.communication.refresh_from_db()
        self.assertEqual(self.communication.status, 'sent')

    def test_delivery_receipt_updates_status(self):
        response = self._post({
            'MessageSid': 'SM-outbound-1',
            'MessageStatus': 'delivered',
            'To': '+14165559999',
        })
        self.assertEqual(response.status_code, 204)

        self.communication.refresh_from_db()
        self.assertEqual(self.communication.status, 'delivered')
        self.assertIsNotNone(self.communication.delivered_at)

    def test_undelivered_receipt_records_the_twilio_error(self):
        self._post({
            'MessageSid': 'SM-outbound-1',
            'MessageStatus': 'undelivered',
            'ErrorCode': '30007',
        })

        self.communication.refresh_from_db()
        self.assertEqual(self.communication.status, 'failed')
        self.assertIn('30007', self.communication.error_message)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.sms_opted_out)

    def test_unsubscribed_error_code_opts_customer_out(self):
        self._post({
            'MessageSid': 'SM-outbound-1',
            'MessageStatus': 'failed',
            'ErrorCode': '21610',
        })

        self.communication.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(self.communication.status, 'failed')
        self.assertTrue(self.customer.sms_opted_out)
        self.assertIsNotNone(self.customer.sms_opted_out_at)

    def test_inbound_reply_answers_with_empty_twiml(self):
        """A TwiML App / number webhook logs error 12300 unless it gets TwiML."""
        response = self._post({
            'MessageSid': 'SM-inbound-twiml',
            'SmsStatus': 'received',
            'From': '+14165559999',
            'To': '+14165550000',
            'Body': 'hello',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/xml')
        body = response.content.decode()
        self.assertIn('<Response', body)
        self.assertNotIn('<Message', body)

    def test_inbound_reply_is_stored_against_the_customer(self):
        response = self._post({
            'MessageSid': 'SM-inbound-1',
            'SmsStatus': 'received',
            'From': '+14165559999',
            'To': '+14165550000',
            'Body': 'when is my payment due',
        })
        self.assertEqual(response.status_code, 200)

        inbound = Communication.objects.get(direction='inbound', type='sms')
        self.assertEqual(inbound.customer_id, self.customer.id)
        self.assertEqual(inbound.content, 'when is my payment due')
        self.assertEqual(inbound.incoming_status, 'new')
        self.assertFalse(inbound.is_unknown_sender)
        self.assertTrue(
            ActivityHistory.objects.filter(
                customer=self.customer, type='sms_received'
            ).exists()
        )

    def test_stop_reply_opts_out_and_start_opts_back_in(self):
        self._post({
            'MessageSid': 'SM-inbound-stop',
            'SmsStatus': 'received',
            'From': '+14165559999',
            'To': '+14165550000',
            'Body': 'STOP',
        })
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.sms_opted_out)

        self._post({
            'MessageSid': 'SM-inbound-start',
            'SmsStatus': 'received',
            'From': '+14165559999',
            'To': '+14165550000',
            'Body': 'START',
        })
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.sms_opted_out)
        self.assertIsNone(self.customer.sms_opted_out_at)

    def test_inbound_from_unknown_number_is_flagged_not_dropped(self):
        self._post({
            'MessageSid': 'SM-inbound-unknown',
            'SmsStatus': 'received',
            'From': '+14165550000',
            'To': '+14165550000',
            'Body': 'hello',
        })

        inbound = Communication.objects.get(direction='inbound', type='sms')
        self.assertIsNone(inbound.customer_id)
        self.assertTrue(inbound.is_unknown_sender)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, **TWILIO_SETTINGS)
class SendSmsEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            email='sms-staff@example.com',
            password='password123',
            full_name='SMS Staff',
            user_type='staff',
            is_staff=True,
        )
        self.customer = Customer.objects.create(
            first_name='Endpoint',
            last_name='Customer',
            email='endpoint-sms@example.com',
            phone='4165558888',
            phone_normalized='4165558888',
            province='ON',
            status='pending',
        )
        self.client.force_authenticate(self.staff)

    @patch('twilio.rest.Client')
    def test_staff_can_send_a_text(self, mock_client):
        mock_client.return_value.messages.create.return_value = _twilio_message('SM9')

        response = self.client.post(
            '/api/communications/send_sms/',
            {'customer_id': str(self.customer.id), 'content': 'Hello there'},
            format='json',
        )

        self.assertEqual(response.status_code, 201, response.data)
        communication = Communication.objects.get(id=response.data['id'])
        self.assertEqual(communication.type, 'sms')
        self.assertEqual(communication.direction, 'outbound')
        self.assertTrue(
            ActivityHistory.objects.filter(
                customer=self.customer, type='sms_sent'
            ).exists()
        )

    def test_endpoint_blocks_opted_out_customer(self):
        self.customer.sms_opted_out = True
        self.customer.save(update_fields=['sms_opted_out'])

        response = self.client.post(
            '/api/communications/send_sms/',
            {'customer_id': str(self.customer.id), 'content': 'Hello there'},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('opted out', response.data['error'])
        self.assertFalse(Communication.objects.filter(type='sms').exists())


class CommunicationTemplateApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            email='template-staff@example.com',
            password='password123',
            full_name='Template Staff',
            user_type='staff',
            is_staff=True,
        )
        self.customer = Customer.objects.create(
            first_name='Shane',
            last_name='Cote',
            email='shane@example.com',
            phone='4165557777',
            phone_normalized='4165557777',
            province='ON',
            status='pending',
        )
        from communications.models import CommunicationTemplate
        self.template, _ = CommunicationTemplate.objects.update_or_create(
            name='Template API Test Email',
            type='email',
            defaults={
                'trigger': 'manual',
                'hot_key': '099',
                'subject': 'Funds sent',
                'content': 'Hi {{customer_first_name}}, funds were sent by EFT.',
                'is_active': True,
            },
        )
        self.client.force_authenticate(self.staff)

    def test_list_includes_hot_key_and_supports_lookup(self):
        listed = self.client.get('/api/communication-templates/', {'type': 'email'})
        self.assertEqual(listed.status_code, 200)
        self.assertTrue(isinstance(listed.data, list))
        names = {row['name'] for row in listed.data}
        self.assertIn('Template API Test Email', names)

        by_key = self.client.get(
            '/api/communication-templates/by-hot-key/',
            {'hot_key': '099'},
        )
        self.assertEqual(by_key.status_code, 200)
        self.assertEqual(by_key.data['id'], str(self.template.id))
        self.assertEqual(by_key.data['hot_key'], '099')

    def test_preview_renders_customer_placeholders(self):
        response = self.client.post(
            f'/api/communication-templates/{self.template.id}/preview/',
            {'customer_id': str(self.customer.id)},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('Shane', response.data['content'])
        self.assertEqual(response.data['subject'], 'Funds sent')

    def test_create_normalizes_blank_hot_key(self):
        response = self.client.post(
            '/api/communication-templates/',
            {
                'name': 'Custom Manual',
                'type': 'email',
                'trigger': 'manual',
                'hot_key': '   ',
                'subject': 'Hello',
                'content': 'Body',
                'is_active': True,
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data['hot_key'])
