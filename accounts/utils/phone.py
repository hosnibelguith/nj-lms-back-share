import re
from rest_framework import serializers


def normalize_ca_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")

    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]

    if len(digits) != 10:
        raise serializers.ValidationError("Enter a valid Canadian phone number.")

    return f"+1{digits}"
