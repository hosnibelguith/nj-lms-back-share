"""
Simplified Loan Serializers.
"""
from decimal import Decimal

from rest_framework import serializers
from .models import (
    CollectionPayment,
    CollectionsAccountChangeAudit,
    FundedPayment,
    Loan,
    LoanFormula,
    LoanStateEvent,
    Payment,
    WebhookEvent,
    FundingMethodRecommendation,
)
from accounts.serializers import CustomerSerializer
from banking.serializers import BankAccountSerializer


class LoanFormulaSerializer(serializers.ModelSerializer):
    brokerage_fee = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    subtotal = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    annual_interest_rate = serializers.DecimalField(max_digits=6, decimal_places=2, read_only=True)
    total_repayable = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = LoanFormula
        fields = [
            'id',
            'name',
            'loan_type',
            'principal_amount',
            'brokerage_percent',
            'brokerage_fee',
            'subtotal',
            'repayment_percent',
            'annual_interest_rate',
            'default_number_of_payments',
            'default_frequency_days',
            'total_repayable',
            'is_active',
            'is_default',
            'notes',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'brokerage_fee',
            'subtotal',
            'annual_interest_rate',
            'total_repayable',
            'created_at',
            'updated_at',
        ]


class FundingMethodRecommendationSerializer(serializers.ModelSerializer):
    weekday_display = serializers.CharField(source='get_weekday_display', read_only=True)
    method_display = serializers.CharField(source='get_method_display', read_only=True)

    class Meta:
        model = FundingMethodRecommendation
        fields = [
            'id',
            'weekday',
            'weekday_display',
            'method',
            'method_display',
            'is_active',
            'notes',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class PaymentSerializer(serializers.ModelSerializer):
    """Payment serializer."""
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    type_display = serializers.CharField(source='get_type_display', read_only=True)

    class Meta:
        model = Payment
        fields = [
            'id', 'loan', 'amount', 'type', 'type_display',
            'status', 'status_display', 'scheduled_date',
            'processed_at', 'failure_reason', 'reference', 'notes',
            'created_at', 'created_by'
        ]
        read_only_fields = ['id', 'created_at', 'processed_at']


class PaymentCreateSerializer(serializers.ModelSerializer):
    """Create payment."""
    class Meta:
        model = Payment
        fields = ['loan', 'amount', 'type', 'scheduled_date', 'notes']


class CustomerLoanPaymentSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    method = serializers.CharField(source='get_type_display', read_only=True)
    balance_after = serializers.SerializerMethodField()
    original_amount = serializers.SerializerMethodField()
    is_deferral_fee = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            'id',
            'amount',
            'original_amount',
            'status',
            'status_display',
            'scheduled_date',
            'processed_at',
            'failure_reason',
            'reference',
            'method',
            'balance_after',
            'is_deferral_fee',
            'notes',
        ]

    def get_original_amount(self, obj):
        """Installment amount before the current edit session (defaults to amount)."""
        return obj.amount

    def get_is_deferral_fee(self, obj):
        from .services import LoanService
        return LoanService.is_deferral_fee_payment(obj)

    def get_balance_after(self, obj):
        """
        Remaining loan total after this installment in schedule order.
        Uses all non-cancelled payments (including scheduled) so the table
        declines correctly before any collections are completed.
        """
        from .services import LoanService

        loan = obj.loan
        cache = self.context.setdefault('_balance_after_by_loan', {})
        loan_key = str(loan.id)
        if loan_key not in cache:
            money = LoanService.money
            payments = list(
                loan.payments.exclude(status='cancelled')
                .order_by('scheduled_date', 'created_at', 'id')
                .only('id', 'amount', 'scheduled_date', 'created_at')
            )
            running_balance = money(loan.total_amount or Decimal('0.00'))
            mapping = {}
            for payment in payments:
                running_balance = money(
                    running_balance - money(payment.amount or Decimal('0.00'))
                )
                mapping[payment.id] = max(running_balance, Decimal('0.00'))
            cache[loan_key] = mapping

        mapped = cache[loan_key].get(obj.id)
        if mapped is not None:
            return mapped
        return max(LoanService.money(loan.balance or Decimal('0.00')), Decimal('0.00'))


class FundedPaymentSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    method_display = serializers.CharField(source='get_method_display', read_only=True)

    class Meta:
        model = FundedPayment
        fields = [
            'id',
            'loan',
            'amount',
            'method',
            'method_display',
            'status',
            'status_display',
            'reference',
            'processor_transaction_id',
            'zum_status',
            'destination_snapshot',
            'collections_account_snapshot',
            'initiated_by',
            'initiated_at',
            'completed_at',
            'failure_reason',
            'notes',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'processor_transaction_id',
            'zum_status',
            'destination_snapshot',
            'collections_account_snapshot',
            'initiated_by',
            'initiated_at',
            'completed_at',
            'created_at',
            'updated_at',
        ]


class CollectionPaymentSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = CollectionPayment
        fields = [
            'id',
            'loan',
            'payment',
            'processor_transaction_id',
            'amount',
            'status',
            'status_display',
            'zum_status',
            'account_snapshot',
            'initiated_at',
            'initiated_by',
            'settlement_due_at',
            'settled_at',
            'failure_reason',
            'event_history',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields


class CollectionsAccountChangeAuditSerializer(serializers.ModelSerializer):
    class Meta:
        model = CollectionsAccountChangeAudit
        fields = [
            'id',
            'loan',
            'previous_account',
            'new_account',
            'changed_by',
            'changed_at',
            'failed_payment',
            'failure_reason',
        ]
        read_only_fields = fields


class WebhookEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookEvent
        fields = [
            'id',
            'processor_transaction_id',
            'webhook_type',
            'event_name',
            'payload',
            'received_at',
            'processed_at',
        ]
        read_only_fields = fields


class CurrentApplicationSerializer(serializers.ModelSerializer):
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    ai_decision_display = serializers.CharField(source='get_ai_decision_display', read_only=True)
    formula = LoanFormulaSerializer(read_only=True)
    contract_signed = serializers.BooleanField(read_only=True)

    collected_amount = serializers.SerializerMethodField()

    class Meta:
        model = Loan
        fields = [
            'id',
            'type',
            'type_display',
            'status',
            'status_display',
            'ai_decision',
            'ai_decision_display',
            'formula',
            'principal',
            'fee',
            'total_amount',
            'balance',
            'collected_amount',
            'is_active',
            'funded_at',
            'approved_at',
            'declined_at',
            'decline_reason',
            'contract_id',
            'contract_sent_at',
            'contract_signed',
            'contract_signed_at',
            'created_at',
            'updated_at',
        ]

    def get_collected_amount(self, obj):
        return max(obj.total_amount - obj.balance, 0)


class CustomerLoanDetailSerializer(serializers.ModelSerializer):
    amount = serializers.DecimalField(source='total_amount', max_digits=10, decimal_places=2, read_only=True)
    collected_amount = serializers.SerializerMethodField()
    collectedAmount = serializers.SerializerMethodField(method_name='get_collected_amount')
    funded_at = serializers.SerializerMethodField()
    fundedAt = serializers.SerializerMethodField(method_name='get_funded_at')
    paymentSchedule = CustomerLoanPaymentSerializer(source='payments', many=True, read_only=True)
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    ai_decision_display = serializers.CharField(source='get_ai_decision_display', read_only=True)
    contract_signed = serializers.BooleanField(read_only=True)

    class Meta:
        model = Loan
        fields = [
            'id',
            'type',
            'type_display',
            'status',
            'status_display',
            'ai_decision',
            'ai_decision_display',
            'is_active',
            'amount',
            'principal',
            'fee',
            'balance',
            'collected_amount',
            'collectedAmount',
            'funded_at',
            'fundedAt',
            'contract_signed',
            'contract_signed_at',
            'contract_sent_at',
            'paymentSchedule',
            'created_at',
            'updated_at',
        ]

    def get_collected_amount(self, obj):
        return max(obj.total_amount - obj.balance, 0)

    def get_funded_at(self, obj):
        return obj.funded_at.isoformat() if obj.funded_at else None


class LoanSerializer(serializers.ModelSerializer):
    """Full loan serializer with nested data."""
    customer = CustomerSerializer(read_only=True)
    customer_id = serializers.UUIDField(write_only=True, source='customer.id', required=False)
    formula = LoanFormulaSerializer(read_only=True)
    bank_account = BankAccountSerializer(read_only=True)
    collections_account = BankAccountSerializer(read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    ai_decision_display = serializers.CharField(source='get_ai_decision_display', read_only=True)
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    contract_signed = serializers.BooleanField(read_only=True)

    class Meta:
        model = Loan
        fields = [
            'id', 'customer', 'customer_id',
            'type', 'type_display', 'principal', 'fee', 'total_amount', 'balance',
            'status', 'status_display', 'is_active',
            'ai_decision', 'ai_decision_display',
            'formula',
            'bank_account',
            'collections_account',
            'funding_method', 'funding_reference', 'funded_at',
            'funding_destination',
            'funding_destination_locked_at',
            'collections_account_locked_at',
            'contract_id', 'contract_sent_at', 'contract_signed', 'contract_signed_at',
            'approved_at', 'approved_by', 'declined_at', 'decline_reason',
            'notes', 'payments',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'total_amount', 'balance', 'funded_at',
            'funding_destination_locked_at',
            'collections_account_locked_at',
            'contract_sent_at', 'contract_signed_at',
            'approved_at', 'approved_by', 'declined_at',
            'created_at', 'updated_at'
        ]


class LoanListSerializer(serializers.ModelSerializer):
    """List serializer aligned with current loans page."""
    customer_name = serializers.SerializerMethodField()
    customer_email = serializers.EmailField(source='customer.email', read_only=True)
    customer_phone = serializers.CharField(source='customer.phone', read_only=True)
    customer_province = serializers.SerializerMethodField()
    customer_banking_verified = serializers.BooleanField(source='customer.banking_verified', read_only=True)
    customer_source = serializers.CharField(source='customer.source', read_only=True)
    is_arrive = serializers.SerializerMethodField()
    amount = serializers.DecimalField(source='total_amount', max_digits=10, decimal_places=2, read_only=True)
    formula = LoanFormulaSerializer(read_only=True)
    funded_date = serializers.SerializerMethodField()
    due_date = serializers.SerializerMethodField()
    collected_amount = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    ai_decision_display = serializers.CharField(source='get_ai_decision_display', read_only=True)
    ibv_status = serializers.SerializerMethodField()
    ibv_status_display = serializers.SerializerMethodField()
    contract_signed = serializers.SerializerMethodField()
    has_funding_failure = serializers.SerializerMethodField()
    funding_failure_reason = serializers.SerializerMethodField()

    class Meta:
        model = Loan
        fields = [
            'id',
            'customer_id',
            'customer_name',
            'customer_email',
            'customer_phone',
            'customer_province',
            'customer_banking_verified',
            'customer_source',
            'is_arrive',
            'type',
            'amount',
            'principal',
            'total_amount',
            'balance',
            'formula',
            'funded_date',
            'due_date',
            'collected_amount',
            'status',
            'status_display',
            'ai_decision',
            'ai_decision_display',
            'ibv_status',
            'ibv_status_display',
            'contract_signed',
            'contract_signed_at',
            'is_active',
            'has_funding_failure',
            'funding_failure_reason',
            'created_at',
        ]

    def get_customer_name(self, obj):
        return getattr(obj.customer, 'full_name', None) or f"{obj.customer.first_name} {obj.customer.last_name}".strip()

    def get_customer_province(self, obj):
        return getattr(obj.customer, 'province', None)

    def get_is_arrive(self, obj):
        customer = obj.customer
        return bool(
            getattr(customer, 'source', None) == 'arrive'
            or getattr(customer, 'arrive_application_id', None)
        )

    def get_funded_date(self, obj):
        if not obj.funded_at:
            return None
        return obj.funded_at.date().isoformat()

    def get_due_date(self, obj):
        payment = obj.payments.order_by('-scheduled_date').first()
        if payment and payment.scheduled_date:
            return payment.scheduled_date.isoformat()
        return None

    def get_collected_amount(self, obj):
        return max(obj.total_amount - obj.balance, 0)

    def get_ibv_status(self, obj):
        return 'completed' if obj.customer.banking_verified else 'pending'

    def get_ibv_status_display(self, obj):
        return 'IBV Completed' if obj.customer.banking_verified else 'Pending IBV'

    def get_contract_signed(self, obj):
        return obj.contract_signed

    def get_has_funding_failure(self, obj):
        # Only surface on loans still awaiting funding (not after a later success).
        if obj.status != 'pending_funding':
            return False
        status_value = getattr(obj, 'funding_failure_status', None)
        reason = getattr(obj, 'funding_failure_reason', None)
        return bool(status_value or reason)

    def get_funding_failure_reason(self, obj):
        if obj.status != 'pending_funding':
            return None
        reason = getattr(obj, 'funding_failure_reason', None)
        status_value = getattr(obj, 'funding_failure_status', None)
        if reason:
            return reason
        if status_value:
            return str(status_value).capitalize()
        return None


class LoanCreateSerializer(serializers.ModelSerializer):
    """Create loan."""
    class Meta:
        model = Loan
        fields = ['customer', 'type', 'principal', 'fee', 'bank_account', 'notes', 'formula']

    def create(self, validated_data):
        principal = validated_data.get('principal', 0)
        fee = validated_data.get('fee', 0)
        validated_data['total_amount'] = principal + fee
        validated_data['balance'] = principal + fee
        customer = validated_data.get('customer')
        if customer and not validated_data.get('status'):
            if not customer.banking_verified:
                validated_data['status'] = 'ibv_pending'
            elif not customer.contract_completed:
                validated_data['status'] = 'pending_signature'
            else:
                validated_data['status'] = 'pending'
        return super().create(validated_data)


class LoanStateEventSerializer(serializers.ModelSerializer):
    event_type_display = serializers.CharField(source='get_event_type_display', read_only=True)
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = LoanStateEvent
        fields = [
            'id',
            'event_type',
            'event_type_display',
            'previous_status',
            'new_status',
            'notes',
            'created_at',
            'created_by',
            'created_by_name',
        ]

    def get_created_by_name(self, obj):
        if obj.created_by:
            return getattr(obj.created_by, 'full_name', None) or getattr(obj.created_by, 'email', None)
        return None


class LoanReactivateSerializer(serializers.Serializer):
    notes = serializers.CharField(required=False, allow_blank=True)


# ----- Action Serializers -----

class LoanApproveSerializer(serializers.Serializer):
    """Approve loan."""
    bank_account_id = serializers.UUIDField(required=False)
    collections_account_id = serializers.UUIDField(required=False)


class LoanDeclineSerializer(serializers.Serializer):
    """Decline loan with a fixed reason label (+ optional staff comment)."""

    ALLOWED_REASONS = (
        'too many loans',
        'already rejected',
        'no job',
        'too many NSF',
        'in collection',
        'see comments',
        'no capacity',
        'loan already in progress',
        'stopped payments',
        'new bank account',
        'new job',
        'Unacceptable bank',
        'Unsupported bank',
    )

    reason = serializers.ChoiceField(choices=[(r, r) for r in ALLOWED_REASONS])
    comment = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class LoanAmountUpdateSerializer(serializers.Serializer):
    """Update approved loan principal before funding."""
    principal = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.01'))
    notes = serializers.CharField(required=False, allow_blank=True)


class LoanScheduleAdjustSerializer(serializers.Serializer):
    """Reprice and replace the scheduled repayment calendar."""
    calculation_mode = serializers.ChoiceField(
        choices=['payment_amount', 'number_of_payments'],
        required=False,
        default='payment_amount',
    )
    payment_amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal('0.01'),
        required=False,
    )
    number_of_payments = serializers.IntegerField(
        min_value=1,
        max_value=260,
        required=False,
    )
    frequency = serializers.ChoiceField(choices=['weekly', 'bi-weekly', 'monthly'])
    start_date = serializers.DateField()
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        mode = attrs.get('calculation_mode') or 'payment_amount'
        if mode == 'payment_amount' and attrs.get('payment_amount') is None:
            raise serializers.ValidationError({
                'payment_amount': 'Payment amount is required when adjusting by amount.'
            })
        if mode == 'number_of_payments' and attrs.get('number_of_payments') is None:
            raise serializers.ValidationError({
                'number_of_payments': 'Number of payments is required when adjusting by payment count.'
            })
        return attrs


class PaymentScheduleItemUpdateSerializer(serializers.Serializer):
    """Edit a single open installment (date and/or amount)."""
    scheduled_date = serializers.DateField(required=False)
    amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal('0.01'),
        required=False,
    )

    def validate(self, attrs):
        if attrs.get('scheduled_date') is None and attrs.get('amount') is None:
            raise serializers.ValidationError(
                'Provide scheduled_date and/or amount to update.'
            )
        return attrs


class PaymentDeferSerializer(serializers.Serializer):
    """Body is optional; defer always creates a scheduled $35 fee payment."""


class PaymentMarkPaidSerializer(serializers.Serializer):
    """Mark a $35 deferral-fee payment as paid."""
    method = serializers.ChoiceField(
        choices=['etransfer', 'manual'],
        required=False,
        default='etransfer',
    )
    reference = serializers.CharField(required=False, allow_blank=True)


class LoanFundSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=['etransfer', 'eft', 'card_issuance'])
    reference = serializers.CharField(required=False, allow_blank=True, max_length=100)
    funding_destination = serializers.DictField(required=False)
    collections_account_id = serializers.UUIDField(required=False)
    schedule_confirmed = serializers.BooleanField(required=True)
    override_confirmed = serializers.BooleanField(required=False, default=False)

    def validate_schedule_confirmed(self, value):
        if not value:
            raise serializers.ValidationError(
                'Schedule must be confirmed before funding.'
            )
        return value


class LoanFundingConfigurationSerializer(serializers.Serializer):
    emt_email = serializers.EmailField(required=False)
    emt_source = serializers.ChoiceField(
        choices=['application', 'flinks'],
        required=False,
    )
    eft_bank_account_id = serializers.UUIDField(required=False)
    collections_account_id = serializers.UUIDField(required=False)


class RecordPaymentSerializer(serializers.Serializer):
    """Record manual payment."""
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    type = serializers.ChoiceField(choices=['manual', 'etransfer'], default='manual')
    reference = serializers.CharField(required=False, max_length=100, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class CollectionInitiateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    payment_id = serializers.UUIDField(required=False)


class CollectionsAccountUpdateSerializer(serializers.Serializer):
    bank_account_id = serializers.UUIDField(required=True)
    failed_payment_id = serializers.UUIDField(required=True)
