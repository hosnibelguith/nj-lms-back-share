# communications/serializers.py
from rest_framework import serializers
from .models import Communication, CommunicationTemplate


class CommunicationHistorySerializer(serializers.ModelSerializer):
    """PRD-shaped serializer for communication history."""
    customer_name = serializers.SerializerMethodField()
    sender = serializers.SerializerMethodField()
    recipient = serializers.SerializerMethodField()
    timestamp = serializers.DateTimeField(source='created_at', read_only=True)
    body = serializers.CharField(source='content', read_only=True)

    class Meta:
        model = Communication
        fields = [
            'id', 'customer', 'customer_name', 'type', 'direction',
            'subject', 'sender', 'recipient', 'status', 'incoming_status',
            'is_answered', 'is_unknown_sender', 'opened_at', 'answered_at',
            'opened_by', 'error_message',
            'timestamp', 'body'
        ]

    def get_customer_name(self, obj):
        if obj.customer:
            return obj.customer.full_name
        if obj.is_unknown_sender:
            return 'User not found'
        return None

    def get_sender(self, obj):
        if obj.type == 'email':
            return obj.from_address
        return obj.from_phone or obj.from_address

    def get_recipient(self, obj):
        if obj.type == 'email':
            return obj.to_address
        return obj.to_phone or obj.to_address


class CommunicationSerializer(serializers.ModelSerializer):
    """Serializer for communications."""
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    direction_display = serializers.CharField(source='get_direction_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    customer_name = serializers.SerializerMethodField()
    
    class Meta:
        model = Communication
        fields = [
            'id', 'customer', 'customer_name', 'loan',
            'type', 'type_display', 'direction', 'direction_display',
            'subject', 'from_address', 'to_address', 'from_phone', 'to_phone',
            'content', 'html_content', 'status', 'status_display',
            'external_id', 'error_message', 'incoming_status', 'is_answered', 'is_unknown_sender',
            'sent_at', 'delivered_at', 'read_at', 'opened_at', 'answered_at',
            'opened_by', 'created_at',
            'template_name', 'created_by'
        ]
        read_only_fields = ['id', 'created_at']

    def get_customer_name(self, obj):
        if obj.customer:
            return obj.customer.full_name
        if obj.is_unknown_sender:
            return 'User not found'
        return None


class CommunicationListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for communication lists."""
    customer_name = serializers.SerializerMethodField()
    
    class Meta:
        model = Communication
        fields = [
            'id', 'customer', 'customer_name', 'loan',
            'type', 'direction', 'subject', 'status', 'is_unknown_sender', 'created_at'
        ]

    def get_customer_name(self, obj):
        if obj.customer:
            return obj.customer.full_name
        if obj.is_unknown_sender:
            return 'User not found'
        return None


class SendEmailSerializer(serializers.Serializer):
    """Serializer for sending emails."""
    customer_id = serializers.UUIDField()
    loan_id = serializers.UUIDField(required=False)
    to_address = serializers.EmailField(required=False, help_text="Override customer email")
    subject = serializers.CharField(max_length=500)
    content = serializers.CharField()
    html_content = serializers.CharField(required=False)
    template_id = serializers.UUIDField(required=False)


class SendCommunicationEmailSerializer(serializers.Serializer):
    """PRD-shaped serializer for sending an email from the communications tab."""
    customer_id = serializers.UUIDField(required=False)
    recipient = serializers.EmailField()
    subject = serializers.CharField(max_length=500)
    body = serializers.CharField()
    loan_id = serializers.UUIDField(required=False)


class ReplyCommunicationSerializer(serializers.Serializer):
    """Serializer for replying to an inbound communication."""
    body = serializers.CharField()


class SendSMSSerializer(serializers.Serializer):
    """Serializer for sending SMS."""
    customer_id = serializers.UUIDField()
    loan_id = serializers.UUIDField(required=False)
    to_phone = serializers.CharField(required=False, help_text="Override customer phone")
    content = serializers.CharField(max_length=1600)
    template_id = serializers.UUIDField(required=False)


class CommunicationTemplateSerializer(serializers.ModelSerializer):
    """Serializer for communication templates."""
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    trigger_display = serializers.CharField(source='get_trigger_display', read_only=True)
    
    class Meta:
        model = CommunicationTemplate
        fields = [
            'id', 'name', 'type', 'type_display', 'trigger', 'trigger_display',
            'hot_key', 'subject', 'content', 'html_content', 'is_active',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate_hot_key(self, value):
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None


class PreviewTemplateSerializer(serializers.Serializer):
    """Serializer for previewing templates."""
    template_id = serializers.UUIDField()
    customer_id = serializers.UUIDField(required=False)
    loan_id = serializers.UUIDField(required=False)
    custom_variables = serializers.DictField(required=False)
