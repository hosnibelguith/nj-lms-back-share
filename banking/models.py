from django.db import models
from accounts.models import Customer
import uuid


class BankConnection(models.Model):
    PROVIDER_CHOICES = [
        ('flinks', 'Flinks'),
    ]

    SYNC_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('syncing', 'Syncing'),
        ('synced', 'Synced'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='bank_connections'
    )
    login_id = models.CharField(max_length=255, unique=True, help_text="Flinks Login ID")
    provider = models.CharField(max_length=50, choices=PROVIDER_CHOICES, default='flinks')

    is_active = models.BooleanField(default=True)
    sync_status = models.CharField(max_length=20, choices=SYNC_STATUS_CHOICES, default='pending')
    sync_error = models.TextField(blank=True, null=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'banking_bankconnection'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.customer.full_name} - {self.provider}"


class BankAccount(models.Model):
    ACCOUNT_TYPE_CHOICES = [
        ('checking', 'Checking'),
        ('savings', 'Savings'),
        ('credit', 'Credit'),
        ('loan', 'Loan'),
        ('investment', 'Investment'),
        ('other', 'Other'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(
        BankConnection,
        on_delete=models.CASCADE,
        related_name='accounts'
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='bank_accounts'
    )

    external_id = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    type = models.CharField(max_length=50, default='other')
    currency = models.CharField(max_length=10, default='CAD')
    balance = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    transit_number = models.CharField(max_length=10, blank=True, null=True)
    institution_number = models.CharField(max_length=10, blank=True, null=True)
    account_number = models.CharField(max_length=20, blank=True, null=True)

    is_primary = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'banking_bankaccount'
        ordering = ['-is_primary', 'name']
        unique_together = ('customer', 'external_id')

    def __str__(self):
        return f"{self.name} ({self.currency})"


class BankTransaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        BankAccount,
        on_delete=models.CASCADE,
        related_name='transactions'
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='bank_transactions'
    )

    external_id = models.CharField(max_length=255)
    date = models.DateField()
    description = models.TextField()

    debit = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    credit = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    balance = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'banking_banktransaction'
        ordering = ['-date', '-created_at']
        unique_together = ('account', 'external_id')

    def __str__(self):
        return f"{self.description} ({self.date})"


class FinancialAnalysisReport(models.Model):
    """
    Keep this simple for now.
    One latest report per customer can be generated later from transactions.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='financial_reports'
    )
    report_data = models.JSONField(default=dict, blank=True)
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'banking_financialanalysisreport'
        ordering = ['-generated_at']

    def __str__(self):
        return f"Financial Report - {self.customer.full_name}"