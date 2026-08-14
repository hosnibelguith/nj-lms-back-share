# communications/tasks.py
from celery import shared_task
from django.utils import timezone
from django.conf import settings
import logging
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def _setting_bool(key: str, default: bool) -> bool:
    from accounts.models import GlobalSetting

    raw = GlobalSetting.get_value(key, str(default))
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _setting_int(key: str, default: int) -> int:
    from accounts.models import GlobalSetting

    try:
        return int(GlobalSetting.get_value(key, str(default)))
    except (TypeError, ValueError):
        return default


def _workflow_reminder_already_sent_today(loan, template_name: str, today) -> bool:
    return loan.communications.filter(
        direction="outbound",
        type="email",
        template_name=template_name,
        created_at__date=today,
    ).exists()


def _workflow_reminder_timezone() -> str:
    from accounts.models import GlobalSetting

    return GlobalSetting.get_value(
        "LOAN_WORKFLOW_REMINDER_TIMEZONE",
        getattr(settings, "LOAN_WORKFLOW_REMINDER_TIMEZONE", "America/New_York"),
    ) or "America/New_York"


def _queue_workflow_reminder(loan, template_name: str, extra_context: dict, today) -> bool:
    from communications.models import CommunicationTemplate

    max_days = _setting_int(
        "LOAN_WORKFLOW_REMINDER_MAX_DAYS",
        getattr(settings, "LOAN_WORKFLOW_REMINDER_MAX_DAYS", 3),
    )
    sent_count = loan.communications.filter(
        direction="outbound",
        type="email",
        template_name=template_name,
    ).count()
    if sent_count >= max_days:
        return False
    if _workflow_reminder_already_sent_today(loan, template_name, today):
        return False

    template = CommunicationTemplate.objects.filter(
        name=template_name,
        type="email",
        is_active=True,
    ).first()
    if not template:
        logger.warning("Workflow reminder template missing: %s", template_name)
        return False

    send_template_message.delay(
        str(loan.customer_id),
        str(template.id),
        str(loan.id),
        extra_context={
            "reminder_number": sent_count + 1,
            "reminder_max_days": max_days,
            **extra_context,
        },
    )
    return True


def _expire_ibv_application(loan, today) -> bool:
    from communications.models import CommunicationTemplate

    template_name = "Application Expired Template"
    previous_status = loan.status
    loan.status = "expired"
    loan.is_active = False
    loan.save(update_fields=["status", "is_active", "updated_at"])
    loan.log_state_event(
        event_type="expired",
        previous_status=previous_status,
        new_status=loan.status,
        notes="IBV application expired after the reminder window.",
    )

    if not _workflow_reminder_already_sent_today(loan, template_name, today):
        template = CommunicationTemplate.objects.filter(
            name=template_name,
            type="email",
            is_active=True,
        ).first()
        if template:
            frontend_url = getattr(settings, "FRONTEND_URL", "http://localhost:3000").rstrip("/")
            send_template_message.delay(
                str(loan.customer_id),
                str(template.id),
                str(loan.id),
                extra_context={"portal_url": f"{frontend_url}/customer/login"},
            )
        else:
            logger.warning("Workflow reminder template missing: %s", template_name)
    return True


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
    from django.core.mail import EmailMultiAlternatives, get_connection
    from accounts.models import GlobalSetting

    try:
        username = GlobalSetting.get_value("EMAIL_HOST_USER", settings.EMAIL_HOST_USER) or None
        password = GlobalSetting.get_value("EMAIL_HOST_PASSWORD", settings.EMAIL_HOST_PASSWORD) or None
        connection = get_connection(username=username, password=password)
        message = EmailMultiAlternatives(
            subject=subject,
            body=content,
            from_email=settings.DEFAULT_FROM_EMAIL or username,
            to=[to],
            connection=connection,
        )
        if html_content:
            message.attach_alternative(html_content, "text/html")
        message.send(fail_silently=False)
        return True, None, None
    except Exception as e:
        return False, None, str(e)


@shared_task
def poll_inbound_email():
    """
    Poll configured inbound email inbox and store matched customer emails.
    """
    if not getattr(settings, "INBOUND_EMAIL_POLL_ENABLED", False):
        logger.info("Inbound email polling disabled.")
        return {
            "skipped": True,
            "reason": "INBOUND_EMAIL_POLL_ENABLED is false",
        }

    from .services.inbound_email import poll_configured_inbound_emails

    result = poll_configured_inbound_emails(limit=settings.INBOUND_EMAIL_POLL_LIMIT)
    logger.info("Inbound email poll result: %s", result.as_dict())
    return result.as_dict()


@shared_task
def send_loan_workflow_reminders():
    """
    Send daily IBV/signature workflow reminders.

    The PeriodicTask schedule is seeded at 8:30 AM America/New_York and can be
    edited in django-celery-beat admin. GlobalSetting keys control enablement
    and max reminder days.
    """
    if not _setting_bool(
        "LOAN_WORKFLOW_REMINDERS_ENABLED",
        getattr(settings, "LOAN_WORKFLOW_REMINDERS_ENABLED", True),
    ):
        logger.info("Loan workflow reminders disabled.")
        return {"skipped": True, "reason": "disabled"}

    from loans.models import Loan

    frontend_url = getattr(settings, "FRONTEND_URL", "http://localhost:3000").rstrip("/")
    tz_name = _workflow_reminder_timezone()
    try:
        today = timezone.localtime(timezone.now(), ZoneInfo(tz_name)).date()
    except Exception:
        logger.warning("Invalid workflow reminder timezone %s; using America/New_York", tz_name)
        today = timezone.localtime(timezone.now(), ZoneInfo("America/New_York")).date()

    ibv_sent = 0
    ibv_expired = 0
    signature_sent = 0

    ibv_loans = Loan.objects.select_related("customer").filter(
        status="ibv_pending",
        customer__banking_verified=False,
        is_active=True,
    )
    for loan in ibv_loans:
        max_days = _setting_int(
            "LOAN_WORKFLOW_REMINDER_MAX_DAYS",
            getattr(settings, "LOAN_WORKFLOW_REMINDER_MAX_DAYS", 3),
        )
        sent_count = loan.communications.filter(
            direction="outbound",
            type="email",
            template_name="IBV Reminder Template",
        ).count()
        if sent_count >= max_days:
            if _expire_ibv_application(loan, today):
                ibv_expired += 1
            continue

        if _queue_workflow_reminder(
            loan,
            "IBV Reminder Template",
            {"portal_url": f"{frontend_url}/customer/banking"},
            today,
        ):
            ibv_sent += 1

    signature_loans = Loan.objects.select_related("customer").filter(
        status="pending_funding",
        approved_at__isnull=False,
        contract_signed_at__isnull=True,
        is_active=True,
    )
    for loan in signature_loans:
        if _queue_workflow_reminder(
            loan,
            "Contract Signature Reminder Template",
            {"portal_url": f"{frontend_url}/customer/contracts"},
            today,
        ):
            signature_sent += 1

    result = {"ibv_sent": ibv_sent, "ibv_expired": ibv_expired, "signature_sent": signature_sent}
    logger.info("Loan workflow reminders queued: %s", result)
    return result


@shared_task(bind=True, max_retries=3)
def send_sms(self, communication_id: str):
    """Send an SMS through Twilio."""
    from .models import Communication
    from .twilio_sms import (
        TwilioConfigurationError,
        TwilioRequestError,
        TwilioService,
        is_opt_out_error,
        set_sms_opt_out,
    )

    try:
        communication = Communication.objects.select_related('customer').get(id=communication_id)
    except Communication.DoesNotExist:
        logger.error('SMS communication not found: %s', communication_id)
        return

    if communication.status != 'pending':
        logger.warning('Communication %s not pending, skipping', communication_id)
        return

    customer = communication.customer
    if customer and customer.sms_opted_out:
        communication.status = 'failed'
        communication.error_message = 'Customer has opted out of SMS.'
        communication.save(update_fields=['status', 'error_message'])
        logger.warning('SMS %s blocked: customer opted out', communication_id)
        return

    try:
        external_id = TwilioService.send_sms(
            to=communication.to_phone,
            content=communication.content,
        )
    except TwilioConfigurationError as exc:
        # Misconfiguration will not fix itself on retry.
        communication.status = 'failed'
        communication.error_message = str(exc)
        communication.save(update_fields=['status', 'error_message'])
        logger.error('SMS %s failed (configuration): %s', communication_id, exc)
        return
    except TwilioRequestError as exc:
        communication.status = 'failed'
        communication.error_message = str(exc)
        communication.save(update_fields=['status', 'error_message'])
        logger.error('SMS %s rejected by Twilio: %s', communication_id, exc)
        if is_opt_out_error(exc.code):
            # Twilio already knows this number must not be texted; mirror it
            # locally so staff see the block instead of retrying forever.
            set_sms_opt_out(customer, opted_out=True, reason=str(exc))
            return
        raise self.retry(exc=exc, countdown=300)
    except Exception as exc:
        communication.status = 'failed'
        communication.error_message = str(exc)
        communication.save(update_fields=['status', 'error_message'])
        logger.error('SMS %s failed: %s', communication_id, exc)
        raise self.retry(exc=exc, countdown=300)

    communication.status = 'sent'
    communication.sent_at = timezone.now()
    communication.external_id = external_id
    communication.error_message = None
    communication.save(
        update_fields=['status', 'sent_at', 'external_id', 'error_message']
    )
    logger.info('SMS %s sent external_id=%s', communication_id, external_id)


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
                    'loan_amount': str(loan.principal),
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
