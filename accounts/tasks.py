# accounts/tasks.py
"""
Celery tasks for accounts app.
"""
from celery import shared_task
from django.utils import timezone
from django.conf import settings
from datetime import timedelta
import logging

logger = logging.getLogger(__name__)


@shared_task
def send_welcome_email(customer_id: str):
    """Send welcome email to new customer."""
    try:
        from django.core.mail import send_mail

        from .models import Customer

        customer = Customer.objects.get(id=customer_id)
        frontend_url = settings.FRONTEND_URL.rstrip('/')

        send_mail(
            subject='Welcome to Mohawk Loans',
            message=(
                f'Hello {customer.first_name},\n\n'
                f'Thank you for applying with Mohawk Loans.\n\n'
                f'Your next step is to complete banking verification:\n'
                f'{frontend_url}/customer/banking\n\n'
                f'Thank you,\nMohawk Loans'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[customer.email],
            fail_silently=False,
        )

        logger.info(f"Welcome email sent to customer {customer_id}")

    except Customer.DoesNotExist:
        logger.error(f"Customer {customer_id} not found")
    except Exception as e:
        logger.error(f"Error sending welcome email: {e}")


@shared_task
def cleanup_inactive_customers():
    """
    Legacy no-op.

    Customer statuses no longer include an inactive state. Future business
    rules should decide how customers move between business statuses.
    """
    logger.info("Skipped inactive customer cleanup; inactive status is no longer supported")
    return 0


@shared_task
def export_customers_report(user_id: str, filters: dict = None):
    """
    Generate and export customers report.
    """
    from .models import Customer, User
    from django.core.mail import EmailMessage
    import csv
    import io
    
    try:
        user = User.objects.get(id=user_id)
        
        # Build queryset with filters
        queryset = Customer.objects.all()
        
        if filters:
            if filters.get('status'):
                queryset = queryset.filter(status=filters['status'])
            if filters.get('province'):
                queryset = queryset.filter(province=filters['province'])
            if filters.get('date_from'):
                queryset = queryset.filter(created_at__gte=filters['date_from'])
            if filters.get('date_to'):
                queryset = queryset.filter(created_at__lte=filters['date_to'])
        
        # Generate CSV
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Header
        writer.writerow([
            'ID', 'First Name', 'Last Name', 'Email', 'Phone',
            'Province', 'Status', 'Created At'
        ])
        
        # Data
        for customer in queryset:
            writer.writerow([
                str(customer.id),
                customer.first_name,
                customer.last_name,
                customer.email,
                customer.phone,
                customer.province,
                customer.status,
                customer.created_at.isoformat()
            ])
        
        # Send email with attachment
        email = EmailMessage(
            subject='Customers Export Report',
            body='Please find attached the customers export report.',
            to=[user.email]
        )
        email.attach('customers_report.csv', output.getvalue(), 'text/csv')
        email.send()
        
        logger.info(f"Customers report sent to {user.email}")
        
    except User.DoesNotExist:
        logger.error(f"User {user_id} not found")
    except Exception as e:
        logger.error(f"Error generating customers report: {e}")
        raise


@shared_task(bind=True, max_retries=3)
def send_sms_otp_task(self, phone_number: str, code: str):
    try:
        from django.conf import settings

        from communications.twilio_sms import TwilioService

        if settings.DEBUG and getattr(settings, "DEV_OTP_CODE", ""):
            logger.warning("DEV OTP for %s is %s", phone_number, code)
            return

        TwilioService.send_sms(
            to=phone_number,
            content=f"Your verification code is {code}. It expires in 10 minutes.",
        )

        logger.info(f"OTP SMS sent to {phone_number}")

    except Exception as e:
        logger.error(f"Error sending OTP SMS: {e}")
        if settings.DEBUG:
            logger.warning("Skipping SMS retry in DEBUG. OTP for %s is %s", phone_number, code)
            return
        raise self.retry(exc=e, countdown=60)


@shared_task(bind=True, max_retries=3)
def send_email_otp_task(self, email: str, code: str):
    try:
        from django.conf import settings
        from django.core.mail import send_mail

        send_mail(
            subject="Your login code",
            message=f"Your login code is {code}. It expires in 10 minutes.",
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )

        logger.info(f"OTP email sent to {email}")

    except Exception as e:
        logger.error(f"Error sending OTP email: {e}")
        raise self.retry(exc=e, countdown=60)


@shared_task(bind=True, max_retries=5, default_retry_delay=30)
def send_arrive_decision_webhook_task(self, loan_id: str, decision: str):
    from accounts.arrive_integration import deliver_decision_webhook

    try:
        ok = deliver_decision_webhook(loan_id, decision)
        if not ok:
            raise RuntimeError(f"Arrive webhook delivery failed for loan {loan_id}")
        return True
    except Exception as exc:
        logger.warning(
            "Arrive webhook attempt failed loan=%s decision=%s retry=%s",
            loan_id,
            decision,
            self.request.retries,
        )
        raise self.retry(exc=exc, countdown=min(300, 30 * (2 ** self.request.retries)))
