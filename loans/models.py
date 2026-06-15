# loans/models.py
"""
Simplified Loan Models.
Just 2 models: Loan and Payment.
Loan lifecycle: pending → pending_signature → pending_funding → active → paid_off (or defaulted)
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import models
from django.utils import timezone
from accounts.models import Customer
from banking.models import BankAccount
import uuid


class LoanFormula(models.Model):
    """
    Configurable pricing formula for loans.
    Example:
    principal 500
    + 70% brokerage = 850 subtotal
    + 29% on subtotal = 1096.50 total repayable
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=255)
    loan_type = models.CharField(max_length=20, choices=[
        ('nojuice', 'NoJuice'),
        ('payday', 'Payday'),
    ], default='nojuice')

    principal_amount = models.DecimalField(max_digits=10, decimal_places=2)

    brokerage_percent = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('70.00'))
    repayment_percent = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('29.00'))

    default_number_of_payments = models.PositiveIntegerField(default=4)
    default_frequency_days = models.PositiveIntegerField(default=14)

    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)

    notes = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'loans_loan_formula'
        ordering = ['principal_amount', 'name']

    def __str__(self):
        return f"{self.name} - ${self.principal_amount}"

    def money(self, value):
        return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def brokerage_fee(self):
        return self.money(
            self.principal_amount * self.brokerage_percent / Decimal('100')
        )

    @property
    def subtotal(self):
        return self.money(self.principal_amount + self.brokerage_fee)

    @property
    def annual_interest_rate(self):
        return self.repayment_percent

    @property
    def total_repayable(self):
        """
        Display estimate only.
        Real repayable amount is calculated from the generated schedule.
        """
        return self.subtotal


class Loan(models.Model):
    """
    Main loan entity - handles entire loan lifecycle.
    """
    # Loan Types
    TYPE_CHOICES = [
        ('nojuice', 'NoJuice'),
        ('payday', 'Payday'),
    ]
    
    # Loan Status - Linear lifecycle
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('pending_signature', 'Pending Signature'),

        ('ai_approved', 'AI Approved'),
        ('ai_declined', 'AI Declined'),
        ('review_required', 'Review Required'),

        ('human_approved', 'Human Approved'),
        ('human_declined', 'Human Declined'),

        ('pending_funding', 'Pending Funding'),

        ('active', 'Active'),
        ('paid_off', 'Paid Off'),
        ('defaulted', 'In Collections'),
    ]
    
    # Funding Methods
    FUNDING_METHOD_CHOICES = [
        ('etransfer', 'Interac e-Transfer'),
        ('eft', 'EFT / Direct Deposit'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='loans')
    formula = models.ForeignKey(
        LoanFormula,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='loans',
        help_text='Pricing formula used to calculate this loan'
    )
    
    # Loan Details
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='nojuice')
    principal = models.DecimalField(max_digits=10, decimal_places=2, help_text="Amount borrowed")
    fee = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text="Total fee charged")
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, help_text="Principal + Fee")
    balance = models.DecimalField(max_digits=10, decimal_places=2, help_text="Remaining balance")
    
    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    is_active = models.BooleanField(default=True)
    # Banking (for funding and collections)
    bank_account = models.ForeignKey(
        BankAccount, 
        on_delete=models.SET_NULL, 
        null=True, blank=True,
        related_name='loans',
        help_text="Account used for funding and PAD collections"
    )
    
    # Funding Details
    funding_method = models.CharField(max_length=20, choices=FUNDING_METHOD_CHOICES, blank=True, null=True)
    funding_reference = models.CharField(max_length=100, blank=True, null=True)
    funded_at = models.DateTimeField(null=True, blank=True)
    
    # Contract Details
    contract_id = models.CharField(max_length=100, blank=True, null=True, help_text="DocuSign envelope ID")
    contract_sent_at = models.DateTimeField(null=True, blank=True)
    contract_signed_at = models.DateTimeField(null=True, blank=True)
    
    # Approval/Decline
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL, 
        null=True, blank=True, related_name='approved_loans'
    )
    declined_at = models.DateTimeField(null=True, blank=True)
    decline_reason = models.TextField(blank=True, null=True)
    
    # Notes
    notes = models.TextField(blank=True, null=True, help_text="Internal notes")
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'loans_loan'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Loan #{str(self.id)[:8]} - {self.customer} - ${self.principal}"
    
    def save(self, *args, **kwargs):
        # Auto-calculate total_amount if not set
        if not self.total_amount:
            self.total_amount = self.principal + self.fee
        # Initialize balance to total_amount for new loans
        if not self.balance:
            self.balance = self.total_amount
        super().save(*args, **kwargs)
    
    # ----- Actions -----

    def log_state_event(self, event_type, previous_status=None, new_status=None, user=None, notes=''):
        LoanStateEvent.objects.create(
            loan=self,
            event_type=event_type,
            previous_status=previous_status,
            new_status=new_status,
            created_by=user,
            notes=notes or '',
        )
    
    def approve(self, user=None, source='human'):
        """Approve the loan by AI or human."""
        previous_status = self.status

        self.status = 'ai_approved' if source == 'ai' else 'human_approved'
        self.is_active = True
        self.approved_at = timezone.now()
        self.approved_by = user
        self.save()

        self.log_state_event(
            event_type=self.status,
            previous_status=previous_status,
            new_status=self.status,
            user=user,
        )
    
    def decline(self, reason, user=None, source='human'):
        """Decline the loan by AI or human."""
        previous_status = self.status

        self.status = 'ai_declined' if source == 'ai' else 'human_declined'
        self.is_active = False
        self.declined_at = timezone.now()
        self.decline_reason = reason
        self.save()

        self.log_state_event(
            event_type=self.status,
            previous_status=previous_status,
            new_status=self.status,
            user=user,
            notes=reason,
        )
    
    def mark_contract_sent(self, contract_id=None):
        """Move loan to pending customer signature."""
        previous_status = self.status

        self.status = 'pending_signature'
        self.is_active = True
        if contract_id:
            self.contract_id = contract_id
        self.contract_sent_at = timezone.now()
        self.save()

        self.log_state_event(
            event_type='pending_signature',
            previous_status=previous_status,
            new_status=self.status,
            notes='Contract/signature request prepared.',
        )
    
    def mark_contract_signed(self):
        """Mark customer agreement as signed."""
        previous_status = self.status

        self.contract_signed_at = timezone.now()

        if self.status in ['ai_approved', 'human_approved']:
            self.status = 'pending_funding'
        else:
            self.status = 'pending'

        self.is_active = True
        self.save()

        self.log_state_event(
            event_type='contract_signed',
            previous_status=previous_status,
            new_status=self.status,
            notes='Customer signed agreement.',
        )
    
    def fund(self, method, reference, user=None):
        """Mark loan as funded/active."""
        previous_status = self.status

        if self.status not in ['pending_funding', 'human_approved', 'ai_approved']:
            raise ValueError(f"Cannot fund loan in status: {self.status}")

        self.status = 'active'
        self.is_active = True
        self.funding_method = method
        self.funding_reference = reference
        self.funded_at = timezone.now()
        self.save()

        self.log_state_event(
            event_type='funded',
            previous_status=previous_status,
            new_status='active',
            user=user,
            notes=reference,
        )
    
    def apply_payment(self, amount, user=None):
        """Apply a payment to the loan balance."""
        previous_status = self.status
        self.balance = max(0, self.balance - amount)

        paid_off_now = False
        if self.balance == 0 and self.status != 'paid_off':
            self.status = 'paid_off'
            self.is_active = False
            paid_off_now = True

        self.save()

        if paid_off_now:
            self.log_state_event(
                event_type='paid_off',
                previous_status=previous_status,
                new_status=self.status,
                user=user,
            )
    
    def mark_defaulted(self, user=None, notes=''):
        """Mark loan as defaulted."""
        previous_status = self.status
        self.status = 'defaulted'
        self.is_active = False
        self.save()

        self.log_state_event(
            event_type='defaulted',
            previous_status=previous_status,
            new_status=self.status,
            user=user,
            notes=notes,
        )

    def reactivate(self, user=None, notes=''):
        """Reactivate a defaulted/inactive loan."""
        previous_status = self.status
        self.status = 'active'
        self.is_active = True
        self.save()

        self.log_state_event(
            event_type='reactivated',
            previous_status=previous_status,
            new_status=self.status,
            user=user,
            notes=notes,
        )


class Payment(models.Model):
    """
    Single payment record - both scheduled (PAD) and actual payments.
    """
    # Payment Types
    TYPE_CHOICES = [
        ('scheduled', 'Scheduled PAD'),       # Pre-authorized debit
        ('manual', 'Manual Payment'),          # Agent-recorded payment
        ('etransfer', 'e-Transfer Received'),  # Customer sent e-transfer
    ]
    
    # Payment Status
    STATUS_CHOICES = [
        ('scheduled', 'Scheduled'),    # Future payment
        ('pending', 'Pending'),        # Processing
        ('completed', 'Completed'),    # Successfully collected
        ('failed', 'Failed'),          # Failed to collect
        ('nsf', 'NSF'),                # Non-sufficient funds
        ('cancelled', 'Cancelled'),    # Cancelled by agent
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name='payments')
    
    # Payment Details
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='scheduled')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='scheduled')
    scheduled_date = models.DateField(help_text="Date payment is scheduled for")
    
    # Processing
    processed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True, null=True)
    reference = models.CharField(max_length=100, blank=True, null=True)
    
    # Notes
    notes = models.TextField(blank=True, null=True)
    
    # Audit
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='created_payments'
    )
    
    class Meta:
        db_table = 'loans_payment'
        ordering = ['scheduled_date', '-created_at']
    
    def __str__(self):
        return f"Payment ${self.amount} for Loan #{str(self.loan_id)[:8]} on {self.scheduled_date}"
    
    def complete(self, user=None):
        """Mark payment as completed and apply to loan."""
        self.status = 'completed'
        self.processed_at = timezone.now()
        self.save()
        # Apply payment to loan balance
        self.loan.apply_payment(self.amount, user=user)
    
    def fail(self, reason=''):
        """Mark payment as failed."""
        self.status = 'failed'
        self.failure_reason = reason
        self.processed_at = timezone.now()
        self.save()
    
    def mark_nsf(self):
        """Mark payment as NSF."""
        self.status = 'nsf'
        self.failure_reason = 'Non-sufficient funds'
        self.processed_at = timezone.now()
        self.save()
    
    def cancel(self):
        """Cancel the payment."""
        self.status = 'cancelled'
        self.save()

class FundedPayment(models.Model):
    """
    Outbound funding transaction sent to the customer for a loan.
    Separate from Payment, which tracks customer repayments/collections.
    """

    METHOD_CHOICES = [
        ('etransfer', 'Interac e-Transfer'),
        ('eft', 'EFT / Direct Deposit'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('returned', 'Returned'),
        ('cancelled', 'Cancelled'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    loan = models.ForeignKey(
        Loan,
        on_delete=models.CASCADE,
        related_name='funded_payments'
    )

    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    reference = models.CharField(max_length=100, blank=True, null=True)
    initiated_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'loans_funded_payment'
        ordering = ['-initiated_at', '-created_at']

    def __str__(self):
        return f"Funded payment ${self.amount} for Loan #{str(self.loan_id)[:8]}"

    def mark_completed(self):
        self.status = 'completed'
        if not self.completed_at:
            self.completed_at = timezone.now()
        self.save(update_fields=['status', 'completed_at', 'updated_at'])

    def mark_failed(self, reason=''):
        self.status = 'failed'
        self.failure_reason = reason
        self.save(update_fields=['status', 'failure_reason', 'updated_at'])



class LoanStateEvent(models.Model):
    """
    Immutable history of important loan state changes for analytics and audit.
    """

    EVENT_CHOICES = [
        ('pending_signature', 'Pending Signature'),
        ('contract_signed', 'Contract Signed'),

        ('ai_approved', 'AI Approved'),
        ('ai_declined', 'AI Declined'),
        ('review_required', 'Review Required'),

        ('human_approved', 'Human Approved'),
        ('human_declined', 'Human Declined'),

        ('funded', 'Funded'),
        ('paid_off', 'Paid Off'),
        ('defaulted', 'Defaulted'),
        ('reactivated', 'Reactivated'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    loan = models.ForeignKey(
        Loan,
        on_delete=models.CASCADE,
        related_name='state_events'
    )
    event_type = models.CharField(max_length=30, choices=EVENT_CHOICES)
    previous_status = models.CharField(max_length=20, blank=True, null=True)
    new_status = models.CharField(max_length=20, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        'accounts.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='loan_state_events'
    )

    class Meta:
        db_table = 'loans_loan_state_event'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.loan_id} - {self.event_type} - {self.created_at}"