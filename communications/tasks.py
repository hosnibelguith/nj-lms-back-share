# communications/tasks.py
from celery import shared_task
from django.utils import timezone
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def send_email(self, communication_id: str):
    """
    Send an email using the configured email provider.
    """
    from .models import Communication
    
    try:
        communication = Communication.objects.select_related('customer').get(id=communication_id)
        
        if communication.status != 'pending':
            logger.warning(f"Communication {communication_id} not pending, skipping")
            return
        
        logger.info(f"Sending email {communication_id} to {communication.to_address}")
        
        # Send via email provider (SendGrid, SES, etc.)
        success, external_id, error = _send_email_via_provider(
            to=communication.to_address,
            subject=communication.subject,
            content=communication.content,
            html_content=communication.html_content
        )
        
        if success:
            communication.status = 'sent'
            communication.sent_at = timezone.now()
            communication.external_id = external_id
        else:
            communication.status = 'failed'
            communication.error_message = error
        
        communication.save()
        
        logger.info(f"Email {communication_id} {'sent' if success else 'failed'}")
        
    except Communication.DoesNotExist:
        logger.error(f"Communication not found: {communication_id}")
    except Exception as e:
        logger.error(f"Error sending email {communication_id}: {str(e)}")
        raise self.retry(exc=e, countdown=300)


def _send_email_via_provider(to: str, subject: str, content: str, html_content: str = None):
    """
    Send email via Django's configured email backend.
    Returns (success: bool, external_id: str, error: str)
    """
    from django.core.mail import EmailMultiAlternatives

    try:
        message = EmailMultiAlternatives(
            subject=subject,
            body=content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[to],
        )
        if html_content:
            message.attach_alternative(html_content, "text/html")
        message.send(fail_silently=False)
        return True, None, None
    except Exception as e:
        return False, None, str(e)


@shared_task(bind=True, max_retries=3)
def send_sms(self, communication_id: str):
    """
    Send an SMS using Twilio.
    """
    from .models import Communication
    
    try:
        communication = Communication.objects.select_related('customer').get(id=communication_id)
        
        if communication.status != 'pending':
            logger.warning(f"Communication {communication_id} not pending, skipping")
            return
        
        logger.info(f"Sending SMS {communication_id} to {communication.to_phone}")
        
        # Send via Twilio
        success, external_id, error = _send_sms_via_twilio(
            to=communication.to_phone,
            content=communication.content
        )
        
        if success:
            communication.status = 'sent'
            communication.sent_at = timezone.now()
            communication.external_id = external_id
        else:
            communication.status = 'failed'
            communication.error_message = error
        
        communication.save()
        
        logger.info(f"SMS {communication_id} {'sent' if success else 'failed'}")
        
    except Communication.DoesNotExist:
        logger.error(f"Communication not found: {communication_id}")
    except Exception as e:
        logger.error(f"Error sending SMS {communication_id}: {str(e)}")
        raise self.retry(exc=e, countdown=300)


def _send_sms_via_twilio(to: str, content: str):
    """
    Send SMS via Twilio.
    Returns (success: bool, external_id: str, error: str)
    
    Note: This is a placeholder. Replace with actual Twilio integration.
    """
    # In production, integrate with Twilio:
    """
    from twilio.rest import Client
    
    client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    
    try:
        message = client.messages.create(
            body=content,
            from_=settings.TWILIO_PHONE_NUMBER,
            to=to
        )
        return True, message.sid, None
    except Exception as e:
        return False, None, str(e)
    """
    
    # For now, simulate success
    import uuid
    logger.info(f"Would send SMS to {to}: {content[:50]}...")
    return True, f"sms-{uuid.uuid4()}", None


@shared_task
def send_bulk_sms(customer_ids: list, message: str, loan_id: str = None):
    """
    Send SMS to multiple customers.
    """
    from accounts.models import Customer
    from .models import Communication
    
    communications = []
    
    for customer_id in customer_ids:
        try:
            customer = Customer.objects.get(id=customer_id)
            
            communication = Communication.objects.create(
                customer=customer,
                loan_id=loan_id,
                type='sms',
                direction='outbound',
                to_phone=customer.phone,
                content=message,
                status='pending'
            )
            
            communications.append(communication)
            
            # Queue individual SMS task
            send_sms.delay(str(communication.id))
            
        except Customer.DoesNotExist:
            logger.warning(f"Customer not found: {customer_id}")
    
    return f"Queued {len(communications)} SMS messages"


@shared_task
def send_bulk_email(customer_ids: list, subject: str, content: str, html_content: str = None, loan_id: str = None):
    """
    Send email to multiple customers.
    """
    from accounts.models import Customer
    from .models import Communication
    
    communications = []
    
    for customer_id in customer_ids:
        try:
            customer = Customer.objects.get(id=customer_id)
            
            communication = Communication.objects.create(
                customer=customer,
                loan_id=loan_id,
                type='email',
                direction='outbound',
                to_address=customer.email,
                subject=subject,
                content=content,
                html_content=html_content,
                status='pending'
            )
            
            communications.append(communication)
            
            # Queue individual email task
            send_email.delay(str(communication.id))
            
        except Customer.DoesNotExist:
            logger.warning(f"Customer not found: {customer_id}")
    
    return f"Queued {len(communications)} emails"


@shared_task
def send_template_message(customer_id: str, template_id: str, loan_id: str = None, extra_context: dict = None):
    """
    Send a message using a template.
    """
    from accounts.models import Customer
    from loans.models import Loan
    from .models import Communication, CommunicationTemplate
    
    try:
        customer = Customer.objects.get(id=customer_id)
        template = CommunicationTemplate.objects.get(id=template_id, is_active=True)
        
        # Build context
        context = {
            'customer_name': customer.full_name,
            'customer_first_name': customer.first_name,
            'customer_email': customer.email,
            'customer_phone': customer.phone,
        }
        
        if loan_id:
            try:
                loan = Loan.objects.get(id=loan_id)
                context.update({
                    'loan_amount': str(loan.amount),
                    'loan_balance': str(loan.balance),
                    'loan_type': loan.type,
                })
            except Loan.DoesNotExist:
                pass
        
        if extra_context:
            context.update(extra_context)
        
        # Render template
        rendered = template.render(context)
        
        # Create communication
        if template.type == 'email':
            communication = Communication.objects.create(
                customer=customer,
                loan_id=loan_id,
                type='email',
                direction='outbound',
                to_address=customer.email,
                subject=rendered['subject'],
                content=rendered['content'],
                html_content=rendered['html_content'],
                status='pending',
                template_name=template.name
            )
            send_email.delay(str(communication.id))
        else:
            communication = Communication.objects.create(
                customer=customer,
                loan_id=loan_id,
                type='sms',
                direction='outbound',
                to_phone=customer.phone,
                content=rendered['content'],
                status='pending',
                template_name=template.name
            )
            send_sms.delay(str(communication.id))
        
        return str(communication.id)
        
    except Customer.DoesNotExist:
        logger.error(f"Customer not found: {customer_id}")
    except CommunicationTemplate.DoesNotExist:
        logger.error(f"Template not found: {template_id}")
