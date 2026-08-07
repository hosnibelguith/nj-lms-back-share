from rest_framework import serializers
from .models import BankConnection, BankAccount, BankTransaction, FinancialAnalysisReport


class BankTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = BankTransaction
        fields = [
            'id',
            'date',
            'description',
            'debit',
            'credit',
            'balance',
            'created_at',
        ]


class BankAccountSerializer(serializers.ModelSerializer):
    transactions = BankTransactionSerializer(many=True, read_only=True)
    is_payment_blocked = serializers.BooleanField(read_only=True)

    class Meta:
        model = BankAccount
        fields = [
            'id',
            'name',
            'type',
            'currency',
            'balance',
            'transit_number',
            'institution_number',
            'account_number',
            'is_primary',
            'is_manual_entry',
            'is_payment_blocked',
            'use_for_eft_funding',
            'use_for_eft_collections',
            'connection',
            'transactions',
        ]


def _digits_only(value: str) -> str:
    return ''.join(ch for ch in str(value or '') if ch.isdigit())


class BankAccountManualCoordinatesSerializer(serializers.Serializer):
    """Institution / transit / account # from a void cheque."""

    institution_number = serializers.CharField(max_length=10)
    transit_number = serializers.CharField(max_length=10)
    account_number = serializers.CharField(max_length=20)
    name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500)

    def validate_institution_number(self, value):
        digits = _digits_only(value)
        if len(digits) != 3:
            raise serializers.ValidationError('Institution number must be 3 digits.')
        # 621/623/703 are allowed with an agent warning in the funding UI — not rejected here.
        return digits

    def validate_transit_number(self, value):
        digits = _digits_only(value)
        if len(digits) != 5:
            raise serializers.ValidationError('Transit number must be 5 digits.')
        return digits

    def validate_account_number(self, value):
        digits = _digits_only(value)
        if not (5 <= len(digits) <= 12):
            raise serializers.ValidationError('Account number must be 5–12 digits.')
        return digits


class ManualBankAccountCreateSerializer(BankAccountManualCoordinatesSerializer):
    customer_id = serializers.UUIDField()
    set_as_primary = serializers.BooleanField(required=False, default=True)


class BankConnectionSerializer(serializers.ModelSerializer):
    accounts = BankAccountSerializer(many=True, read_only=True)

    class Meta:
        model = BankConnection
        fields = [
            'id',
            'provider',
            'is_active',
            'sync_status',
            'sync_error',
            'last_synced_at',
            'created_at',
            'accounts',
        ]


class FinancialAnalysisReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = FinancialAnalysisReport
        fields = ['id', 'report_data', 'generated_at']


class CustomerPortalBankingStatusSerializer(serializers.Serializer):
    banking_verified = serializers.BooleanField()
    onboarding_stage = serializers.CharField()
    has_connection = serializers.BooleanField()
    connection_status = serializers.CharField(allow_null=True)
    last_synced_at = serializers.DateTimeField(allow_null=True)
    account_count = serializers.IntegerField()
    failure_message = serializers.CharField(allow_null=True, required=False)
    failure_reason_code = serializers.CharField(allow_null=True, required=False)
    requires_ibv_refill = serializers.BooleanField(required=False)
