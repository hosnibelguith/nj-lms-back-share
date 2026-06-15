from rest_framework import serializers
from .models import Contract, ContractTemplate


class ContractSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    customer_name = serializers.CharField(source='customer.full_name', read_only=True)
    loan_amount = serializers.DecimalField(source='loan.total_amount', max_digits=12, decimal_places=2, read_only=True)
    is_signed = serializers.BooleanField(read_only=True)

    class Meta:
        model = Contract
        fields = [
            'id',
            'customer',
            'customer_name',
            'loan',
            'loan_amount',
            'status',
            'status_display',
            'agreement_version',
            'agreement_text',
            'typed_name',
            'signer_email',
            'signer_ip',
            'signed_date',
            'accepted_terms',
            'accepted_credit_check',
            'accepted_banking_review',
            'accepted_electronic_signature',
            'is_signed',
            'created_at',
            'updated_at',
            'created_by',
        ]
        read_only_fields = [
            'id',
            'customer',
            'customer_name',
            'loan',
            'loan_amount',
            'status',
            'status_display',
            'agreement_version',
            'agreement_text',
            'signer_email',
            'signer_ip',
            'signed_date',
            'is_signed',
            'created_at',
            'updated_at',
            'created_by',
        ]


class CustomerSignContractSerializer(serializers.Serializer):
    typed_name = serializers.CharField(max_length=255)
    accepted_terms = serializers.BooleanField()
    accepted_credit_check = serializers.BooleanField()
    accepted_banking_review = serializers.BooleanField()
    accepted_electronic_signature = serializers.BooleanField()

    def validate(self, attrs):
        required = [
            'accepted_terms',
            'accepted_credit_check',
            'accepted_banking_review',
            'accepted_electronic_signature',
        ]

        if not all(attrs.get(field) is True for field in required):
            raise serializers.ValidationError(
                'All acknowledgements must be accepted before signing.'
            )

        return attrs


class ContractTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContractTemplate
        fields = [
            'id',
            'name',
            'loan_type',
            'province',
            'version',
            'template_url',
            'is_active',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']