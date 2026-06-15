# communications/models.py
from django.db import models
from accounts.models import Customer
from loans.models import Loan
import uuid


class Communication(models.Model):
    """
    Communication records - emails and SMS sent to/received from customers.
    """
    TYPE_CHOICES = [
        ('email', 'Email'),
        ('sms', 'SMS'),
    ]
    
    DIRECTION_CHOICES = [
        ('inbound', 'Inbound'),
        ('outbound', 'Outbound'),
    ]
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('delivered', 'Delivered'),
        ('read', 'Read'),
        ('failed', 'Failed'),
        ('bounced', 'Bounced'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='communications'
    )
    loan = models.ForeignKey(
        Loan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='communications'
    )
    
    type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)
    
    # Email specific
    subject = models.CharField(max_length=500, blank=True, null=True)
    from_address = models.EmailField(blank=True, null=True)
    to_address = models.EmailField(blank=True, null=True)
    
    # SMS specific
    from_phone = models.CharField(max_length=20, blank=True, null=True)
    to_phone = models.CharField(max_length=20, blank=True, null=True)
    
    # Content
    content = models.TextField()
    html_content = models.TextField(blank=True, null=True)
    
    # Status tracking
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    external_id = models.CharField(max_length=255, blank=True, null=True, help_text="Twilio/SendGrid message ID")
    error_message = models.TextField(blank=True, null=True)
    
    # Timestamps
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    # Metadata
    template_name = models.CharField(max_length=100, blank=True, null=True)
    created_by = models.ForeignKey(
        'accounts.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sent_communications'
    )
    
    class Meta:
        db_table = 'communications_communication'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.type} to {self.customer} - {self.status}"


class CommunicationTemplate(models.Model):
    """
    Templates for automated communications.
    """
    TYPE_CHOICES = [
        ('email', 'Email'),
        ('sms', 'SMS'),
    ]
    
    TRIGGER_CHOICES = [
        ('manual', 'Manual'),
        ('loan_approved', 'Loan Approved'),
        ('loan_funded', 'Loan Funded'),
        ('payment_reminder', 'Payment Reminder'),
        ('payment_due', 'Payment Due'),
        ('payment_failed', 'Payment Failed'),
        ('contract_sent', 'Contract Sent'),
        ('welcome', 'Welcome'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    trigger = models.CharField(max_length=50, choices=TRIGGER_CHOICES, default='manual')
    
    # Email specific
    subject = models.CharField(max_length=500, blank=True, null=True)
    
    # Content (supports template variables like {{customer_name}})
    content = models.TextField()
    html_content = models.TextField(blank=True, null=True)
    
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'communications_communicationtemplate'
        ordering = ['name']
    
    def __str__(self):
        return f"{self.name} ({self.type})"
    
    def render(self, context: dict) -> dict:
        """Render template with context variables."""
        rendered_content = self.content
        rendered_subject = self.subject or ''
        rendered_html = self.html_content or ''
        
        for key, value in context.items():
            placeholder = f"{{{{{key}}}}}"
            rendered_content = rendered_content.replace(placeholder, str(value))
            rendered_subject = rendered_subject.replace(placeholder, str(value))
            rendered_html = rendered_html.replace(placeholder, str(value))
        
        return {
            'subject': rendered_subject,
            'content': rendered_content,
            'html_content': rendered_html,
        }
