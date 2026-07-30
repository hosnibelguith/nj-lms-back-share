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
            'use_for_eft_funding',
            'use_for_eft_collections',
            'connection',
            'transactions',
        ]


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
