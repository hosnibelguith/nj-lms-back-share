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
            subject='Welcome to LendStack',
            message=(
                f'Hello {customer.first_name},\n\n'
                f'Thank you for applying with LendStack.\n\n'
                f'Your next step is to complete banking verification:\n'
                f'{frontend_url}/customer/banking\n\n'
                f'Thank you,\nLendStack'
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
    Mark customers as inactive if they have no active loans
    and no activity in the last 2 years.
    """
    from .models import Customer
    from django.db.models import Q, Max
    
    two_years_ago = timezone.now() - timedelta(days=730)
    
    # Find customers with no recent activity
    inactive_customers = Customer.objects.filter(
        status='active'
    ).exclude(
        loans__status__in=['active', 'funded', 'pending']
    ).annotate(
        last_activity=Max('activities__created_at')
    ).filter(
        Q(last_activity__lt=two_years_ago) | Q(last_activity__isnull=True)
    )
    
    count = inactive_customers.update(status='inactive')
    logger.info(f"Marked {count} customers as inactive")
    
    return count


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
        from twilio.rest import Client

        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)

        client.messages.create(
            body=f"Your verification code is {code}. It expires in 10 minutes.",
            from_=settings.TWILIO_PHONE_NUMBER,
            to=phone_number,
        )

        logger.info(f"OTP SMS sent to {phone_number}")

    except Exception as e:
        logger.error(f"Error sending OTP SMS: {e}")
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
