# activity/serializers.py
from rest_framework import serializers
from .models import ActivityHistory, Comment


class ActivityHistorySerializer(serializers.ModelSerializer):
    """Serializer for activity history."""
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    customer_name = serializers.CharField(source='customer.full_name', read_only=True)
    created_by_name = serializers.SerializerMethodField()
    
    class Meta:
        model = ActivityHistory
        fields = [
            'id', 'customer', 'customer_name', 'loan',
            'type', 'type_display', 'title', 'description',
            'metadata', 'created_by', 'created_by_name', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']
    
    def get_created_by_name(self, obj):
        if not obj.created_by:
            return 'System'
        if obj.created_by == 'system':
            return 'System'
        
        # Try to get user name
        from accounts.models import User
        try:
            user = User.objects.get(id=obj.created_by)
            return user.full_name
        except (User.DoesNotExist, ValueError):
            return obj.created_by


class ActivityHistoryCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating activity entries."""
    
    class Meta:
        model = ActivityHistory
        fields = ['customer', 'loan', 'type', 'title', 'description', 'metadata']


class CommentSerializer(serializers.ModelSerializer):
    """Serializer for comments."""
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    customer_name = serializers.CharField(source='customer.full_name', read_only=True)
    
    class Meta:
        model = Comment
        fields = [
            'id', 'customer', 'customer_name', 'loan',
            'content', 'is_internal', 'is_pinned',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by']
    
    def create(self, validated_data):
        # Set created_by from request user
        if 'created_by' not in validated_data and self.context.get('request'):
            validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)


class CommentCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating comments."""
    
    class Meta:
        model = Comment
        fields = ['customer', 'loan', 'content', 'is_internal', 'is_pinned']


class ActivityTimelineSerializer(serializers.Serializer):
    """Serializer for combined activity timeline."""
    id = serializers.UUIDField()
    type = serializers.CharField()
    type_display = serializers.CharField()
    title = serializers.CharField()
    description = serializers.CharField()
    created_at = serializers.DateTimeField()
    created_by = serializers.CharField()
    created_by_name = serializers.CharField()
    metadata = serializers.DictField()
    source = serializers.CharField()  # 'activity' or 'comment'
