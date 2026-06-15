# communications/views.py
from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Q
from .models import Communication, CommunicationTemplate
from .serializers import (
    CommunicationSerializer, CommunicationListSerializer,
    SendEmailSerializer, SendSMSSerializer,
    CommunicationTemplateSerializer, PreviewTemplateSerializer
)
from .tasks import send_email, send_sms


class CommunicationViewSet(viewsets.ModelViewSet):
    """ViewSet for managing communications."""
    queryset = Communication.objects.all()
    permission_classes = [permissions.IsAuthenticated]
    
    def get_serializer_class(self):
        if self.action == 'list':
            return CommunicationListSerializer
        return CommunicationSerializer
    
    def get_queryset(self):
        queryset = Communication.objects.select_related('customer', 'loan')
        
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
        send_email.delay(str(communication.id))
        
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
        
        to_phone = data.get('to_phone') or customer.phone
        
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


class CommunicationTemplateViewSet(viewsets.ModelViewSet):
    """ViewSet for managing communication templates."""
    queryset = CommunicationTemplate.objects.all()
    serializer_class = CommunicationTemplateSerializer
    permission_classes = [permissions.IsAuthenticated]
    
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
        
        # Filter active only
        active_only = self.request.query_params.get('active_only', 'true')
        if active_only.lower() == 'true':
            queryset = queryset.filter(is_active=True)
        
        return queryset
    
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
                    'loan_amount': str(loan.amount),
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
    """Handle incoming webhooks from Twilio (SMS status updates, inbound SMS)."""
    permission_classes = [permissions.AllowAny]
    
    def post(self, request):
        """Process Twilio webhook."""
        message_sid = request.data.get('MessageSid')
        message_status = request.data.get('MessageStatus')
        from_number = request.data.get('From')
        to_number = request.data.get('To')
        body = request.data.get('Body')
        
        if message_sid and message_status:
            # Status update for outbound message
            try:
                communication = Communication.objects.get(external_id=message_sid)
                
                status_mapping = {
                    'queued': 'pending',
                    'sent': 'sent',
                    'delivered': 'delivered',
                    'read': 'read',
                    'failed': 'failed',
                    'undelivered': 'failed',
                }
                
                communication.status = status_mapping.get(message_status, communication.status)
                
                from django.utils import timezone
                if message_status == 'delivered':
                    communication.delivered_at = timezone.now()
                elif message_status == 'read':
                    communication.read_at = timezone.now()
                
                communication.save()
                
            except Communication.DoesNotExist:
                pass
        
        elif body and from_number:
            # Inbound SMS
            # Find customer by phone number
            from accounts.models import Customer
            
            customer = Customer.objects.filter(phone__icontains=from_number[-10:]).first()
            
            if customer:
                Communication.objects.create(
                    customer=customer,
                    type='sms',
                    direction='inbound',
                    from_phone=from_number,
                    to_phone=to_number,
                    content=body,
                    status='delivered',
                    external_id=message_sid
                )
                
                # Log activity
                from activity.models import ActivityHistory
                ActivityHistory.objects.create(
                    customer=customer,
                    type='sms_received',
                    title='SMS Received',
                    description=f'Received: {body[:100]}...' if len(body) > 100 else f'Received: {body}'
                )
        
        return Response({'status': 'received'})
