from decimal import Decimal

from django.utils.dateparse import parse_date
from rest_framework import serializers

from accounts.utils.phone import normalize_ca_phone


class ArriveCreateLeadSerializer(serializers.Serializer):
    event_id = serializers.CharField(max_length=100)
    arrive_application_id = serializers.CharField(max_length=100)
    zum_user_id = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=30)
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    requested_loan_amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    province = serializers.CharField(max_length=2, required=False, allow_blank=True)
    date_of_birth = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    zum_user_card_id = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)

    def validate_phone(self, value):
        normalize_ca_phone(value)
        return value

    def validate_province(self, value):
        if not value:
            return value
        return value.strip().upper()

    def validate_date_of_birth(self, value):
        if value in (None, ""):
            return None
        parsed = parse_date(str(value))
        if not parsed:
            raise serializers.ValidationError("Use YYYY-MM-DD format.")
        return parsed


class ArrivePortalSessionSerializer(serializers.Serializer):
    arrive_application_id = serializers.CharField(max_length=100)
    zum_user_id = serializers.CharField(max_length=100)
    loan_id = serializers.UUIDField()


class ArriveHandoffSerializer(serializers.Serializer):
    token = serializers.UUIDField()
