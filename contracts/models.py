# contracts/models.py
from django.db import models
from accounts.models import Customer
from loans.models import Loan
import uuid


class Contract(models.Model):
    """
    Custom in-app customer agreement.
    Signed with checkboxes + typed name.
    Legal team can replace agreement_text later.
    """

    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('signed', 'Signed'),
        ('void', 'Void'),
    ]

    AGREEMENT_VERSION = 'mohawk-channel-v2'

    DEFAULT_AGREEMENT_TEXT = (
        'Loan agreement content is generated dynamically at preview/sign time.'
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='contracts'
    )
    loan = models.ForeignKey(
        Loan,
        on_delete=models.CASCADE,
        related_name='contracts'
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')

    agreement_version = models.CharField(max_length=50, default=AGREEMENT_VERSION)
    agreement_text = models.TextField(default=DEFAULT_AGREEMENT_TEXT)

    typed_name = models.CharField(max_length=255, blank=True, null=True)
    signer_email = models.EmailField(blank=True, null=True)
    signer_ip = models.GenericIPAddressField(null=True, blank=True)
    signed_date = models.DateTimeField(null=True, blank=True)

    accepted_terms = models.BooleanField(default=False)
    accepted_credit_check = models.BooleanField(default=False)
    accepted_banking_review = models.BooleanField(default=False)
    accepted_electronic_signature = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        'accounts.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_contracts'
    )

    class Meta:
        db_table = 'contracts_contract'
        ordering = ['-created_at']

    def __str__(self):
        return f"Contract for Loan {self.loan_id} - {self.status}"

    @property
    def is_signed(self):
        return self.status == 'signed'


class ContractTemplate(models.Model):
    """
    Placeholder template model.
    Can be expanded later by legal/compliance team.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=255)
    loan_type = models.CharField(max_length=50, blank=True, null=True)
    province = models.CharField(max_length=10, blank=True, null=True)

    version = models.CharField(max_length=50, default='demo-v1')

    template_url = models.URLField(blank=True, null=True)

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'contracts_contracttemplate'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.version})"