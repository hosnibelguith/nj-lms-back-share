# activity/views.py
from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db.models import Q
from .models import ActivityHistory, Comment
from .serializers import (
    ActivityHistorySerializer, ActivityHistoryCreateSerializer,
    CommentSerializer, CommentCreateSerializer
)


class ActivityHistoryViewSet(viewsets.ModelViewSet):
    """ViewSet for viewing activity history."""
    queryset = ActivityHistory.objects.all()
    permission_classes = [permissions.IsAuthenticated]
    
    def get_serializer_class(self):
        if self.action == 'create':
            return ActivityHistoryCreateSerializer
        return ActivityHistorySerializer
    
    def get_queryset(self):
        queryset = ActivityHistory.objects.select_related('customer', 'loan')
        
        # Filter by customer
        customer_id = self.request.query_params.get('customer_id')
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)
        
        # Filter by loan
        loan_id = self.request.query_params.get('loan_id')
        if loan_id:
            queryset = queryset.filter(loan_id=loan_id)
        
        # Filter by type
        activity_type = self.request.query_params.get('type')
        if activity_type:
            if ',' in activity_type:
                types = activity_type.split(',')
                queryset = queryset.filter(type__in=types)
            else:
                queryset = queryset.filter(type=activity_type)
        
        # Filter by date range
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        if start_date:
            queryset = queryset.filter(created_at__date__gte=start_date)
        if end_date:
            queryset = queryset.filter(created_at__date__lte=end_date)
        
        # Search
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search) |
                Q(description__icontains=search)
            )
        
        return queryset
    
    def perform_create(self, serializer):
        serializer.save(created_by=str(self.request.user.id))
    
    @action(detail=False, methods=['get'])
    def timeline(self, request):
        """
        Get a combined timeline of activities and comments for a customer.
        """
        customer_id = request.query_params.get('customer_id')
        loan_id = request.query_params.get('loan_id')
        limit = int(request.query_params.get('limit', 50))
        
        if not customer_id:
            return Response(
                {'error': 'customer_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get activities only — Comment.save() already creates an ActivityHistory row,
        # so merging Comment objects here would duplicate notes in the timeline.
        activities = ActivityHistory.objects.filter(customer_id=customer_id)
        if loan_id:
            activities = activities.filter(Q(loan_id=loan_id) | Q(loan__isnull=True))

        activities = list(activities[:limit])

        timeline_items = []

        for activity in activities:
            timeline_items.append({
                'id': str(activity.id),
                'type': activity.type,
                'type_display': activity.get_type_display(),
                'title': activity.title,
                'description': activity.description,
                'created_at': activity.created_at,
                'created_by': activity.created_by,
                'created_by_name': self._get_user_name(activity.created_by),
                'loan': str(activity.loan_id) if activity.loan_id else None,
                'metadata': {
                    **(activity.metadata or {}),
                    **({'loan_id': str(activity.loan_id)} if activity.loan_id else {}),
                },
                'source': 'activity',
            })

        # Sort by created_at descending
        timeline_items.sort(key=lambda x: x['created_at'], reverse=True)

        return Response(timeline_items[:limit])
    
    def _get_user_name(self, user_id):
        if not user_id:
            return 'System'
        if user_id == 'system':
            return 'System'
        
        from accounts.models import User
        try:
            user = User.objects.get(id=user_id)
            return user.full_name
        except (User.DoesNotExist, ValueError):
            return user_id


class CommentViewSet(viewsets.ModelViewSet):
    """ViewSet for managing comments."""
    queryset = Comment.objects.all()
    permission_classes = [permissions.IsAuthenticated]
    
    def get_serializer_class(self):
        if self.action == 'create':
            return CommentCreateSerializer
        return CommentSerializer
    
    def get_queryset(self):
        queryset = Comment.objects.select_related('customer', 'loan', 'created_by')
        
        # Filter by customer
        customer_id = self.request.query_params.get('customer_id')
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)
        
        # Filter by loan
        loan_id = self.request.query_params.get('loan_id')
        if loan_id:
            queryset = queryset.filter(loan_id=loan_id)
        
        # Filter pinned only
        pinned_only = self.request.query_params.get('pinned_only')
        if pinned_only and pinned_only.lower() == 'true':
            queryset = queryset.filter(is_pinned=True)
        
        return queryset
    
    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)
    
    @action(detail=True, methods=['post'])
    def pin(self, request, pk=None):
        """Pin a comment."""
        comment = self.get_object()
        comment.is_pinned = True
        comment.save()
        return Response({'message': 'Comment pinned'})
    
    @action(detail=True, methods=['post'])
    def unpin(self, request, pk=None):
        """Unpin a comment."""
        comment = self.get_object()
        comment.is_pinned = False
        comment.save()
        return Response({'message': 'Comment unpinned'})
