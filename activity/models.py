# activity/models.py
from django.db import models
from accounts.models import Customer
from loans.models import Loan
import uuid


class ActivityHistory(models.Model):
    """
    Timeline of all customer/loan-related events.
    Used for audit trail and activity feed.
    """
    TYPE_CHOICES = [
        # Loan events
        ('loan_created', 'Loan Created'),
        ('loan_funded', 'Loan Funded'),
        ('loan_paid_off', 'Loan Paid Off'),
        ('loan_defaulted', 'Loan Defaulted'),
        ('loan_renewed', 'Loan Renewed'),
        
        # Payment events
        ('payment_scheduled', 'Payment Scheduled'),
        ('payment_completed', 'Payment Completed'),
        ('payment_failed', 'Payment Failed'),
        
        # Contract events
        ('contract_signed', 'Contract Signed'),
        ('contract_sent', 'Contract Sent'),
        
        # Banking events
        ('ibv_completed', 'IBV Completed'),
        ('bank_connected', 'Bank Connected'),
        
        # Communication events
        ('comment', 'Comment'),
        ('sms_sent', 'SMS Sent'),
        ('sms_received', 'SMS Received'),
        ('email_sent', 'Email Sent'),
        ('email_received', 'Email Received'),
        
        # Customer events
        ('customer_created', 'Customer Created'),
        ('customer_updated', 'Customer Updated'),
        ('customer_blocked', 'Customer Blocked'),
        
        # System events
        ('system', 'System Event'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='activities'
    )
    loan = models.ForeignKey(
        Loan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='activities'
    )
    
    type = models.CharField(max_length=50, choices=TYPE_CHOICES)
    title = models.CharField(max_length=255)
    description = models.TextField()
    
    # Flexible metadata for different event types
    metadata = models.JSONField(default=dict, blank=True)
    
    # Who/what created this activity
    created_by = models.CharField(max_length=255, blank=True, null=True, help_text="User ID or 'system'")
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'activity_activityhistory'
        ordering = ['-created_at']
        verbose_name_plural = 'Activity histories'
    
    def __str__(self):
        return f"{self.type} - {self.title} ({self.created_at})"


class Comment(models.Model):
    """
    Internal comments/notes on customers or loans.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='comments'
    )
    loan = models.ForeignKey(
        Loan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='comments'
    )
    
    content = models.TextField()
    is_internal = models.BooleanField(default=True, help_text="Internal note not visible to customer")
    is_pinned = models.BooleanField(default=False)
    
    created_by = models.ForeignKey(
        'accounts.User',
        on_delete=models.SET_NULL,
        null=True,
        related_name='comments'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'activity_comment'
        ordering = ['-is_pinned', '-created_at']
    
    def __str__(self):
        return f"Comment on {self.customer} by {self.created_by}"
    
    def save(self, *args, **kwargs):
        is_new = self._state.adding
        super().save(*args, **kwargs)
        
        # Create activity entry for new comments
        if is_new:
            author = None
            if self.created_by_id:
                author = getattr(self.created_by, 'full_name', None) or getattr(
                    self.created_by, 'email', None
                )
            ActivityHistory.objects.create(
                customer=self.customer,
                loan=self.loan,
                type='comment',
                title=f'Comment by {author or "Staff"}',
                description=self.content[:200] + '...' if len(self.content) > 200 else self.content,
                created_by=str(self.created_by_id) if self.created_by_id else None,
                metadata={
                    'comment_id': str(self.id),
                    'loan_id': str(self.loan_id) if self.loan_id else None,
                }
            )
