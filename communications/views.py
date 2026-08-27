# communications/views.py
import logging

from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Q
from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone
from email.utils import parseaddr
from .models import Communication, CommunicationTemplate
from .serializers import (
    CommunicationSerializer, CommunicationListSerializer,
    SendEmailSerializer, SendSMSSerializer,
    CommunicationTemplateSerializer, PreviewTemplateSerializer,
    CommunicationHistorySerializer, SendCommunicationEmailSerializer,
    ReplyCommunicationSerializer
)
from .tasks import send_email as send_email_task, send_sms

logger = logging.getLogger(__name__)


class CommunicationViewSet(viewsets.ModelViewSet):
    """ViewSet for managing communications."""
    queryset = Communication.objects.all()
    permission_classes = [permissions.IsAuthenticated]
    
    def get_serializer_class(self):
        if self.action == 'list':
            return CommunicationListSerializer
        return CommunicationSerializer
    
    def get_queryset(self):
        queryset = Communication.objects.select_related('customer', 'loan', 'created_by')
        
        # Filter by customer
        customer_id = self.request.query_params.get('customer_id')
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)
        
        # Filter by loan
        loan_id = self.request.query_params.get('loan_id')
        if loan_id:
            queryset = queryset.filter(loan_id=loan_id)
        
        # Filter by type
        comm_type = self.request.query_params.get('type')
        if comm_type:
            queryset = queryset.filter(type=comm_type)
        
        # Filter by direction
        direction = self.request.query_params.get('direction')
        if direction:
            queryset = queryset.filter(direction=direction)
        
        # Filter by status
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        # Search
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(subject__icontains=search) |
                Q(content__icontains=search)
            )
        
        return queryset

    @action(detail=False, methods=['get'], pagination_class=None)
    def history(self, request):
        """Return this customer's full communication history.

        The customer Comms tab needs every SMS and email (manual and automated),
        so this action never uses the default 25-row page size. An optional
        ``limit`` still caps the emails inbox, not the customer tab.
        """
        queryset = self.get_queryset()
        limit = request.query_params.get('limit')

        if limit:
            try:
                limit = min(int(limit), 500)
                queryset = queryset[:limit]
            except ValueError:
                pass

        return Response(CommunicationHistorySerializer(queryset, many=True).data)

    @action(detail=False, methods=['get'])
    def incoming(self, request):
        """Return unanswered inbound emails for the Incoming Comms section."""
        queryset = self.get_queryset().filter(
            direction='inbound',
            type='email',
            incoming_status='unanswered',
            is_answered=False,
        )
        limit = request.query_params.get('limit')

        if limit:
            try:
                limit = min(int(limit), 200)
                queryset = queryset[:limit]
            except ValueError:
                pass

        return Response(CommunicationHistorySerializer(queryset, many=True).data)

    @action(detail=False, methods=['get'], url_path='email-summary')
    def email_summary(self, request):
        """Return database-backed email counts for dashboard headers.

        Unanswered counts collapse same-customer / same-day inbound duplicates
        so three follow-ups on one day count as one inbox item.
        """
        from communications.services.inbox_grouping import (
            collapsed_unanswered_email_counts,
        )

        queryset = self.get_queryset().filter(type='email')
        collapsed = collapsed_unanswered_email_counts(queryset)

        return Response({
            'total_count': queryset.count(),
            'unanswered_count': collapsed['unanswered_count'],
            'new_count': collapsed['new_count'],
            'opened_unanswered_count': collapsed['opened_unanswered_count'],
        })

    @action(detail=False, methods=['get'], url_path='new-incoming')
    def new_incoming(self, request):
        """Return new inbound emails for the dashboard notification bar.

        Collapses same-customer / same-day duplicates (newest representative).
        """
        from communications.services.inbox_grouping import group_key_for_inbound

        queryset = self.get_queryset().filter(
            direction='inbound',
            type='email',
            incoming_status='new',
            is_answered=False,
        ).order_by('-created_at')

        seen = set()
        collapsed = []
        for row in queryset.iterator(chunk_size=200):
            key = group_key_for_inbound(row)
            if key in seen:
                continue
            seen.add(key)
            collapsed.append(row)

        limit = request.query_params.get('limit')
        if limit:
            try:
                collapsed = collapsed[: min(int(limit), 200)]
            except ValueError:
                pass

        return Response(CommunicationHistorySerializer(collapsed, many=True).data)

    @action(detail=True, methods=['post'], url_path='mark-opened')
    def mark_opened(self, request, pk=None):
        """Mark an inbound communication as read/opened by the current user."""
        communication = self.get_object()

        if communication.direction != 'inbound':
            return Response(
                {'error': 'Only inbound communications can be marked opened.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        update_fields = []
        if not communication.is_answered and communication.incoming_status != 'unanswered':
            communication.incoming_status = 'unanswered'
            update_fields.append('incoming_status')

        if not communication.opened_at:
            communication.opened_at = timezone.now()
            communication.opened_by = getattr(request.user, 'email', '') or None
            update_fields.extend(['opened_at', 'opened_by'])

        if update_fields:
            communication.save(update_fields=update_fields)
        return Response(CommunicationHistorySerializer(communication).data)

    @action(detail=True, methods=['post'])
    def reply(self, request, pk=None):
        """Reply to an unanswered inbound email and mark it answered on success."""
        communication = self.get_object()
        serializer = ReplyCommunicationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if communication.direction != 'inbound' or communication.type != 'email':
            return Response(
                {'success': False, 'message': 'Only inbound emails can be replied to.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if communication.is_answered:
            return Response(
                {'success': False, 'message': 'This email has already been answered.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if not communication.from_address:
            return Response(
                {'success': False, 'message': 'Incoming email has no reply address.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        subject = communication.subject or ''
        reply_subject = subject if subject.lower().startswith('re:') else f'Re: {subject or "No subject"}'

        reply_communication = Communication.objects.create(
            customer=communication.customer,
            loan=communication.loan,
            type='email',
            direction='outbound',
            subject=reply_subject,
            from_address=parseaddr(settings.DEFAULT_FROM_EMAIL)[1] or settings.EMAIL_HOST_USER,
            to_address=communication.from_address,
            content=serializer.validated_data['body'],
            status='pending',
            created_by=request.user
        )

        try:
            send_email_task(str(reply_communication.id))
            reply_communication.refresh_from_db()
            if reply_communication.status != 'sent':
                error_message = reply_communication.error_message or 'SMTP provider did not accept the reply.'
                return Response(
                    {
                        'success': False,
                        'message': 'Unable to send reply.',
                        'error': error_message,
                    },
                    status=status.HTTP_502_BAD_GATEWAY
                )

            from communications.services.inbox_grouping import (
                mark_same_day_inbound_answered,
            )

            answered_at = timezone.now()
            opened_by = getattr(request.user, 'email', '') or None
            # Reply answers this email and any same-day follow-ups from the
            # same customer/contact so duplicate unanswered rows clear together.
            mark_same_day_inbound_answered(
                communication,
                opened_by=opened_by,
                answered_at=answered_at,
            )
            communication.refresh_from_db()

            return Response({
                'success': True,
                'message': 'Reply sent successfully.'
            })
        except Exception as exc:
            reply_communication.status = 'failed'
            reply_communication.error_message = str(exc)
            reply_communication.save(update_fields=['status', 'error_message'])
            return Response(
                {
                    'success': False,
                    'message': 'Unable to send reply.',
                    'error': str(exc),
                },
                status=status.HTTP_502_BAD_GATEWAY
            )

    @action(detail=False, methods=['post'])
    def send(self, request):
        """Send an email using Django's configured SMTP backend."""
        serializer = SendCommunicationEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        from accounts.models import Customer

        customer = None
        customer_id = data.get('customer_id')
        if customer_id:
            customer = Customer.objects.filter(id=customer_id).first()
        if customer is None:
            customer = Customer.objects.filter(email__iexact=data['recipient']).first()
        if customer is None:
            return Response(
                {'success': False, 'message': 'Customer not found.'},
                status=status.HTTP_404_NOT_FOUND
            )

        communication = Communication.objects.create(
            customer=customer,
            loan_id=data.get('loan_id'),
            type='email',
            direction='outbound',
            subject=data['subject'],
            from_address=parseaddr(settings.DEFAULT_FROM_EMAIL)[1] or settings.EMAIL_HOST_USER,
            to_address=data['recipient'],
            content=data['body'],
            status='pending',
            created_by=request.user
        )

        try:
            send_email_task(str(communication.id))
            communication.refresh_from_db()
            if communication.status == 'sent':
                from activity.models import ActivityHistory
                ActivityHistory.objects.create(
                    customer=customer,
                    loan_id=data.get('loan_id'),
                    type='email_sent',
                    title='Email Sent',
                    description=f'Email sent: {data["subject"]}',
                    created_by=str(request.user.id)
                )
                return Response({
                    'success': True,
                    'message': 'Email sent successfully.'
                })

            error_message = communication.error_message or 'SMTP provider did not accept the email.'
            return Response(
                {
                    'success': False,
                    'message': 'Unable to send email.',
                    'error': error_message,
                },
                status=status.HTTP_502_BAD_GATEWAY
            )
        except Exception as exc:
            communication.status = 'failed'
            communication.error_message = str(exc)
            communication.save(update_fields=['status', 'error_message'])
            return Response(
                {
                    'success': False,
                    'message': 'Unable to send email.',
                    'error': str(exc),
                },
                status=status.HTTP_502_BAD_GATEWAY
            )
    
    @action(detail=False, methods=['post'])
    def send_email(self, request):
        """Send an email to a customer."""
        serializer = SendEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        data = serializer.validated_data
        
        # Get customer
        from accounts.models import Customer
        try:
            customer = Customer.objects.get(id=data['customer_id'])
        except Customer.DoesNotExist:
            return Response(
                {'error': 'Customer not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        to_address = data.get('to_address') or customer.email
        
        # Create communication record
        communication = Communication.objects.create(
            customer=customer,
            loan_id=data.get('loan_id'),
            type='email',
            direction='outbound',
            subject=data['subject'],
            to_address=to_address,
            content=data['content'],
            html_content=data.get('html_content'),
            status='pending',
            created_by=request.user
        )
        
        # Queue email task
        send_email_task.delay(str(communication.id))
        
        # Log activity
        from activity.models import ActivityHistory
        ActivityHistory.objects.create(
            customer=customer,
            loan_id=data.get('loan_id'),
            type='email_sent',
            title='Email Sent',
            description=f'Email sent: {data["subject"]}',
            created_by=str(request.user.id)
        )
        
        return Response(CommunicationSerializer(communication).data, status=status.HTTP_201_CREATED)
    
    @action(detail=False, methods=['post'])
    def send_sms(self, request):
        """Send an SMS to a customer."""
        serializer = SendSMSSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        data = serializer.validated_data
        
        # Get customer
        from accounts.models import Customer
        try:
            customer = Customer.objects.get(id=data['customer_id'])
        except Customer.DoesNotExist:
            return Response(
                {'error': 'Customer not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        if customer.sms_opted_out:
            return Response(
                {'error': 'Customer has opted out of SMS and cannot be texted.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        to_phone = data.get('to_phone') or customer.phone
        if not to_phone:
            return Response(
                {'error': 'Customer has no phone number on file.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Create communication record
        communication = Communication.objects.create(
            customer=customer,
            loan_id=data.get('loan_id'),
            type='sms',
            direction='outbound',
            to_phone=to_phone,
            content=data['content'],
            status='pending',
            created_by=request.user
        )
        
        # Queue SMS task
        send_sms.delay(str(communication.id))
        
        # Log activity
        from activity.models import ActivityHistory
        ActivityHistory.objects.create(
            customer=customer,
            loan_id=data.get('loan_id'),
            type='sms_sent',
            title='SMS Sent',
            description=f'SMS sent to {to_phone}',
            created_by=str(request.user.id)
        )
        
        return Response(CommunicationSerializer(communication).data, status=status.HTTP_201_CREATED)


# Name-based lookups used by loan/account workflow automation — keep these intact.
WORKFLOW_TEMPLATE_NAMES = frozenset({
    'Deny Template',
    'DENIED',
    'Fund/Approve Template',
    'We Have Received Your Request Template',
    'IBV Reminder Template',
    'Contract Signature Reminder Template',
})


class CommunicationTemplateViewSet(viewsets.ModelViewSet):
    """ViewSet for managing communication templates."""
    queryset = CommunicationTemplate.objects.all()
    serializer_class = CommunicationTemplateSerializer
    permission_classes = [permissions.IsAuthenticated]
    # Full list is small and used by the composer dropdown — avoid page truncation.
    pagination_class = None

    def destroy(self, request, *args, **kwargs):
        template = self.get_object()
        if template.name in WORKFLOW_TEMPLATE_NAMES:
            return Response(
                {
                    'error': (
                        'This template is used by loan workflow automation and '
                        'cannot be deleted. Deactivate it only if you intend to '
                        'stop those automated messages.'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().destroy(request, *args, **kwargs)
    
    def get_queryset(self):
        queryset = CommunicationTemplate.objects.all()
        
        # Filter by type
        comm_type = self.request.query_params.get('type')
        if comm_type:
            queryset = queryset.filter(type=comm_type)
        
        # Filter by trigger
        trigger = self.request.query_params.get('trigger')
        if trigger:
            queryset = queryset.filter(trigger=trigger)

        hot_key = (self.request.query_params.get('hot_key') or '').strip()
        if hot_key:
            queryset = queryset.filter(hot_key__iexact=hot_key)

        search = (self.request.query_params.get('search') or '').strip()
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) |
                Q(subject__icontains=search) |
                Q(hot_key__icontains=search)
            )
        
        # Filter active only
        active_only = self.request.query_params.get('active_only', 'true')
        if active_only.lower() == 'true':
            queryset = queryset.filter(is_active=True)
        
        return queryset

    @action(detail=False, methods=['get'], url_path='by-hot-key')
    def by_hot_key(self, request):
        """Resolve a single active template by Hot Key code."""
        hot_key = (request.query_params.get('hot_key') or '').strip()
        if not hot_key:
            return Response(
                {'error': 'hot_key is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        template = CommunicationTemplate.objects.filter(
            hot_key__iexact=hot_key,
            is_active=True,
        ).first()
        if not template:
            return Response(
                {'error': 'Template not found for that Hot Key.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(CommunicationTemplateSerializer(template).data)
    
    @action(detail=True, methods=['post'])
    def preview(self, request, pk=None):
        """Preview a template with sample data."""
        template = self.get_object()
        
        # Build context
        context = request.data.get('custom_variables', {})
        
        customer_id = request.data.get('customer_id')
        if customer_id:
            from accounts.models import Customer
            try:
                customer = Customer.objects.get(id=customer_id)
                context.update({
                    'customer_name': customer.full_name,
                    'customer_first_name': customer.first_name,
                    'customer_email': customer.email,
                    'customer_phone': customer.phone,
                })
            except Customer.DoesNotExist:
                pass
        
        loan_id = request.data.get('loan_id')
        if loan_id:
            from loans.models import Loan
            try:
                loan = Loan.objects.get(id=loan_id)
                context.update({
                    'loan_amount': str(loan.principal),
                    'loan_balance': str(loan.balance),
                    'loan_type': loan.type,
                })
            except Loan.DoesNotExist:
                pass
        
        # Add default placeholders for missing variables
        context.setdefault('customer_name', '[Customer Name]')
        context.setdefault('customer_first_name', '[First Name]')
        context.setdefault('loan_amount', '[Amount]')
        
        rendered = template.render(context)
        
        return Response({
            'subject': rendered['subject'],
            'content': rendered['content'],
            'html_content': rendered['html_content'],
        })


class TwilioWebhookView(APIView):
    """Delivery receipts and inbound replies from Twilio.

    Twilio posts form-encoded data signed with X-Twilio-Signature, so the
    endpoint is gated on that signature rather than DRF authentication.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def _signature_valid(self, request) -> bool:
        from twilio.request_validator import RequestValidator

        from .twilio_sms import TwilioConfigurationError, TwilioService

        try:
            auth_token = TwilioService.auth_token()
        except TwilioConfigurationError:
            logger.error('Twilio webhook rejected: auth token is not configured')
            return False

        signature = request.headers.get('X-Twilio-Signature') or ''
        if not signature:
            return False

        params = request.data
        params = params.dict() if hasattr(params, 'dict') else dict(params)
        return RequestValidator(auth_token).validate(
            request.build_absolute_uri(), params, signature
        )

    @staticmethod
    def _empty_twiml():
        """Acknowledge an inbound message without sending an auto-reply.

        Twilio's incoming-message webhook (number or TwiML App) expects TwiML;
        answering with JSON makes every inbound text log error 12300.
        """
        from twilio.twiml.messaging_response import MessagingResponse

        return HttpResponse(str(MessagingResponse()), content_type='text/xml')

    def post(self, request):
        if not self._signature_valid(request):
            logger.warning('Twilio webhook rejected: invalid signature')
            return Response(
                {'error': 'invalid_signature'}, status=status.HTTP_403_FORBIDDEN
            )

        message_status = str(
            request.data.get('MessageStatus') or request.data.get('SmsStatus') or ''
        ).strip().lower()

        if message_status == 'received':
            self._handle_inbound(request.data)
            return self._empty_twiml()

        if message_status:
            self._handle_status(request.data)
        else:
            logger.info('Twilio webhook ignored: no message status in payload')

        # Status callbacks ignore the body; 204 keeps the ack cheap.
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _handle_status(self, payload):
        from .twilio_sms import is_opt_out_error, map_message_status, set_sms_opt_out

        message_sid = payload.get('MessageSid') or payload.get('SmsSid')
        if not message_sid:
            return False

        communication = Communication.objects.filter(external_id=message_sid).first()
        if communication is None:
            logger.info('Twilio status for unknown message sid=%s', message_sid)
            return False

        mapped = map_message_status(
            payload.get('MessageStatus') or payload.get('SmsStatus')
        )
        error_code = payload.get('ErrorCode')
        failure_reason = payload.get('ErrorMessage') or (
            f'Twilio error {error_code}'
            if error_code
            else 'Twilio reported the message as undeliverable.'
        )
        update_fields = []

        if mapped:
            communication.status = mapped
            update_fields.append('status')
            if mapped == 'delivered' and not communication.delivered_at:
                communication.delivered_at = timezone.now()
                update_fields.append('delivered_at')
            elif mapped == 'read' and not communication.read_at:
                communication.read_at = timezone.now()
                update_fields.append('read_at')
            if mapped == 'failed':
                communication.error_message = failure_reason
                update_fields.append('error_message')

        if update_fields:
            communication.save(update_fields=update_fields)

        if is_opt_out_error(error_code):
            set_sms_opt_out(
                communication.customer, opted_out=True, reason=failure_reason
            )
        return True

    def _handle_inbound(self, payload):
        from accounts.models import Customer
        from activity.models import ActivityHistory

        from .twilio_sms import (
            classify_inbound_keyword,
            phone_last10,
            set_sms_opt_out,
        )

        from_phone = payload.get('From') or ''
        body = payload.get('Body') or ''
        last10 = phone_last10(from_phone)
        if not last10:
            return False

        customer = Customer.objects.filter(phone_normalized__endswith=last10).first()
        if customer is None:
            customer = Customer.objects.filter(phone__contains=last10).first()

        Communication.objects.create(
            customer=customer,
            type='sms',
            direction='inbound',
            from_phone=from_phone,
            to_phone=payload.get('To') or '',
            content=body,
            status='delivered',
            external_id=payload.get('MessageSid') or None,
            incoming_status='new',
            is_unknown_sender=customer is None,
        )

        keyword = classify_inbound_keyword(body)
        if keyword == 'stop':
            set_sms_opt_out(customer, opted_out=True, reason='Customer replied STOP')
        elif keyword == 'start':
            set_sms_opt_out(customer, opted_out=False)

        if customer:
            preview = body if len(body) <= 100 else f'{body[:100]}...'
            ActivityHistory.objects.create(
                customer=customer,
                type='sms_received',
                title='SMS Received',
                description=f'Received: {preview}',
            )
        return True
