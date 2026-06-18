"""
Simplified Loan Serializers.
"""
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

    class Meta:
        model = Payment
        fields = [
            'id',
            'amount',
            'status',
            'status_display',
            'scheduled_date',
            'processed_at',
            'failure_reason',
            'reference',
            'method',
            'balance_after',
        ]

    def get_balance_after(self, obj):
        loan = obj.loan
        payments = list(
            loan.payments.order_by('scheduled_date', 'created_at').only(
                'id', 'amount', 'status', 'scheduled_date', 'created_at'
            )
        )

        running_balance = loan.total_amount
        for payment in payments:
            if payment.status == 'completed':
                running_balance -= payment.amount

            if payment.id == obj.id:
                return max(running_balance, 0)

        return loan.balance


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
    formula = LoanFormulaSerializer(read_only=True)

    collected_amount = serializers.SerializerMethodField()

    class Meta:
        model = Loan
        fields = [
            'id',
            'type',
            'type_display',
            'status',
            'status_display',
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
            'contract_signed_at',
            'created_at',
            'updated_at',
        ]

    def get_collected_amount(self, obj):
        return max(obj.total_amount - obj.balance, 0)


class CustomerLoanDetailSerializer(serializers.ModelSerializer):
    amount = serializers.DecimalField(source='total_amount', max_digits=10, decimal_places=2, read_only=True)
    collectedAmount = serializers.SerializerMethodField()
    fundedAt = serializers.SerializerMethodField()
    paymentSchedule = CustomerLoanPaymentSerializer(source='payments', many=True, read_only=True)
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Loan
        fields = [
            'id',
            'type',
            'type_display',
            'status',
            'status_display',
            'is_active',
            'amount',
            'principal',
            'fee',
            'balance',
            'collectedAmount',
            'fundedAt',
            'paymentSchedule',
            'created_at',
            'updated_at',
        ]

    def get_collectedAmount(self, obj):
        return max(obj.total_amount - obj.balance, 0)

    def get_fundedAt(self, obj):
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
    type_display = serializers.CharField(source='get_type_display', read_only=True)

    class Meta:
        model = Loan
        fields = [
            'id', 'customer', 'customer_id',
            'type', 'type_display', 'principal', 'fee', 'total_amount', 'balance',
            'status', 'status_display', 'is_active',
            'formula',
            'bank_account',
            'collections_account',
            'funding_method', 'funding_reference', 'funded_at',
            'funding_destination',
            'funding_destination_locked_at',
            'collections_account_locked_at',
            'contract_id', 'contract_sent_at', 'contract_signed_at',
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
    customer_province = serializers.SerializerMethodField()
    amount = serializers.DecimalField(source='total_amount', max_digits=10, decimal_places=2, read_only=True)
    formula = LoanFormulaSerializer(read_only=True)
    funded_date = serializers.SerializerMethodField()
    due_date = serializers.SerializerMethodField()
    collected_amount = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Loan
        fields = [
            'id',
            'customer_id',
            'customer_name',
            'customer_province',
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
            'is_active',
            'created_at',
        ]

    def get_customer_name(self, obj):
        return getattr(obj.customer, 'full_name', None) or f"{obj.customer.first_name} {obj.customer.last_name}".strip()

    def get_customer_province(self, obj):
        return getattr(obj.customer, 'province', None)

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


class LoanDeclineSerializer(serializers.Serializer):
    """Decline loan."""
    reason = serializers.CharField(required=True)


class LoanFundSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=['etransfer', 'eft'])
    reference = serializers.CharField(required=False, allow_blank=True, max_length=100)
    funding_destination = serializers.DictField(required=False)
    collections_account_id = serializers.UUIDField(required=False)
    schedule_confirmed = serializers.BooleanField(required=True)

    def validate_schedule_confirmed(self, value):
        if not value:
            raise serializers.ValidationError(
                'Agent must confirm the payment schedule was reviewed before funding.'
            )
        return value


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
    failed_payment_id = serializers.UUIDField(required=False)
