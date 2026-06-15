from rest_framework import viewsets, permissions
from .models import Contract, ContractTemplate
from .serializers import ContractSerializer, ContractTemplateSerializer


class StaffOnlyPermission(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.user_type == 'staff'
        )


class ContractViewSet(viewsets.ModelViewSet):
    queryset = Contract.objects.select_related('customer', 'loan', 'created_by')
    serializer_class = ContractSerializer
    permission_classes = [StaffOnlyPermission]

    def get_queryset(self):
        queryset = super().get_queryset()

        customer_id = self.request.query_params.get('customer_id')
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)

        loan_id = self.request.query_params.get('loan_id')
        if loan_id:
            queryset = queryset.filter(loan_id=loan_id)

        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        return queryset


class ContractTemplateViewSet(viewsets.ModelViewSet):
    queryset = ContractTemplate.objects.all()
    serializer_class = ContractTemplateSerializer
    permission_classes = [StaffOnlyPermission]