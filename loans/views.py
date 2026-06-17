from decimal import Decimal

from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response

from django.utils import timezone
from django.utils.dateparse import parse_date
from django.db.models import Q, Sum, Count
from django.db.models.functions import TruncDate

from .models import Loan, Payment, LoanStateEvent, FundedPayment, LoanFormula
from .services import LoanService
from .serializers import (
    LoanFormulaSerializer,
    LoanSerializer,
    LoanListSerializer,
    LoanCreateSerializer,
    PaymentSerializer,
    PaymentCreateSerializer,
    LoanApproveSerializer,
    LoanDeclineSerializer,
    LoanFundSerializer,
    RecordPaymentSerializer,
    LoanStateEventSerializer,
    LoanReactivateSerializer,
    FundedPaymentSerializer,
)


class StaffOnlyPermission(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.user_type == 'staff'
        )


# =====================================================
# LOANS
# =====================================================


class LoanFormulaViewSet(viewsets.ModelViewSet):
    queryset = LoanFormula.objects.all()
    serializer_class = LoanFormulaSerializer
    permission_classes = [StaffOnlyPermission]

    def get_queryset(self):
        qs = super().get_queryset()

        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() == 'true')

        loan_type = self.request.query_params.get('loan_type')
        if loan_type:
            qs = qs.filter(loan_type=loan_type)

        return qs

class LoanViewSet(viewsets.ModelViewSet):
    queryset = Loan.objects.select_related(
        'customer', 'bank_account', 'approved_by'
    ).prefetch_related('payments', 'state_events')

    permission_classes = [StaffOnlyPermission]

    # -------------------------
    # SERIALIZER SWITCH
    # -------------------------
    def get_serializer_class(self):
        if self.action == 'list':
            return LoanListSerializer
        if self.action == 'create':
            return LoanCreateSerializer
        return LoanSerializer

    # -------------------------
    # QUERY FILTERING
    # -------------------------
    def get_queryset(self):
        qs = super().get_queryset()

        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)

        customer_id = self.request.query_params.get('customer_id')
        if customer_id:
            qs = qs.filter(customer_id=customer_id)

        loan_type = self.request.query_params.get('type')
        if loan_type:
            qs = qs.filter(type=loan_type)

        province = self.request.query_params.get('province')
        if province:
            qs = qs.filter(customer__province=province)

        is_active_param = self.request.query_params.get('is_active')
        if is_active_param is not None:
            normalized = is_active_param.strip().lower()
            if normalized in ['true', '1', 'yes']:
                qs = qs.filter(is_active=True)
            elif normalized in ['false', '0', 'no']:
                qs = qs.filter(is_active=False)

        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(
                Q(id__icontains=search) |
                Q(customer__first_name__icontains=search) |
                Q(customer__last_name__icontains=search) |
                Q(customer__email__icontains=search)
            )

        date_from = self.request.query_params.get('date_from')
        if date_from:
            parsed_from = parse_date(date_from)
            if parsed_from:
                qs = qs.filter(
                    Q(funded_at__date__gte=parsed_from) |
                    Q(funded_at__isnull=True, created_at__date__gte=parsed_from)
                )

        date_to = self.request.query_params.get('date_to')
        if date_to:
            parsed_to = parse_date(date_to)
            if parsed_to:
                qs = qs.filter(
                    Q(funded_at__date__lte=parsed_to) |
                    Q(funded_at__isnull=True, created_at__date__lte=parsed_to)
                )

        ordering = self.request.query_params.get('ordering')
        allowed_ordering = {
            'created_at': 'created_at',
            '-created_at': '-created_at',
            'funded_at': 'funded_at',
            '-funded_at': '-funded_at',
            'total_amount': 'total_amount',
            '-total_amount': '-total_amount',
            'balance': 'balance',
            '-balance': '-balance',
            'status': 'status',
            '-status': '-status',
            'customer_name': 'customer__first_name',
            '-customer_name': '-customer__first_name',
        }
        if ordering in allowed_ordering:
            qs = qs.order_by(allowed_ordering[ordering])

        return qs
    # =====================================================
    # ACTIONS
    # =====================================================

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        loan = self.get_object()

        if loan.status not in ['pending', 'review_required', 'ai_approved', 'ai_declined']:
            return Response({'error': 'Only pending loans can be approved'}, status=400)

        serializer = LoanApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        bank_account_id = serializer.validated_data.get('bank_account_id')
        if bank_account_id:
            loan.bank_account_id = bank_account_id

        loan = LoanService.approve_loan(
            loan=loan,
            approved_by=request.user,
            source='human',
        )

        return Response(LoanSerializer(loan).data)

    @action(detail=True, methods=['post'])
    def decline(self, request, pk=None):
        loan = self.get_object()

        if loan.status not in ['pending', 'pending_signature', 'review_required', 'ai_approved', 'ai_declined', 'human_approved']:
            return Response({'error': 'Only pending loans can be declined'}, status=400)

        serializer = LoanDeclineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        loan = LoanService.decline_loan(
            loan=loan,
            reason=serializer.validated_data['reason'],
            declined_by=request.user,
            source='human',
        )

        return Response(LoanSerializer(loan).data)

    @action(detail=True, methods=['post'])
    def fund(self, request, pk=None):
        loan = self.get_object()

        if loan.status != 'pending_funding':
            return Response({'error': 'Only signed loans can be funded'}, status=400)

        serializer = LoanFundSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            loan = LoanService.fund_loan(
                loan=loan,
                method=serializer.validated_data['method'],
                reference=serializer.validated_data.get('reference', ''),
                user=request.user,
            )
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)

        return Response(LoanSerializer(loan).data)

    @action(detail=True, methods=['post'])
    def record_payment(self, request, pk=None):
        loan = self.get_object()

        if loan.status != 'active':
            return Response({'error': 'Invalid loan state'}, status=400)

        serializer = RecordPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        payment = Payment.objects.create(
            loan=loan,
            amount=serializer.validated_data['amount'],
            type=serializer.validated_data['type'],
            scheduled_date=timezone.now().date(),
            reference=serializer.validated_data.get('reference', ''),
            notes=serializer.validated_data.get('notes', ''),
            created_by=request.user
        )

        payment.status = 'completed'
        payment.processed_at = timezone.now()
        payment.save()

        loan.apply_payment(payment.amount, user=request.user)

        return Response(LoanSerializer(loan).data)

    @action(detail=True, methods=['post'])
    def mark_defaulted(self, request, pk=None):
        loan = self.get_object()

        if loan.status != 'active':
            return Response({'error': 'Only active loans can default'}, status=400)

        loan.mark_defaulted(user=request.user)

        return Response(LoanSerializer(loan).data)

    @action(detail=True, methods=['post'])
    def reactivate(self, request, pk=None):
        loan = self.get_object()

        if loan.is_active:
            return Response({'error': 'Loan already active'}, status=400)

        serializer = LoanReactivateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        loan.reactivate(
            user=request.user,
            notes=serializer.validated_data.get('notes', '')
        )

        return Response(LoanSerializer(loan).data)

    @action(detail=True, methods=['get'], url_path='interest-breakdown')
    def interest_breakdown(self, request, pk=None):
        loan = self.get_object()
        as_of_param = request.query_params.get('as_of')
        as_of = parse_date(as_of_param) if as_of_param else None
        return Response(LoanService.get_interest_breakdown(loan, as_of_date=as_of))

    @action(detail=True, methods=['get'])
    def state_events(self, request, pk=None):
        loan = self.get_object()
        events = loan.state_events.select_related('created_by').all()
        return Response(LoanStateEventSerializer(events, many=True).data)
    @action(detail=True, methods=['get'], url_path='funded-payments')
    def funded_payments(self, request, pk=None):
        loan = self.get_object()
        funded_payments = loan.funded_payments.all().order_by('-initiated_at', '-created_at')
        return Response(FundedPaymentSerializer(funded_payments, many=True).data)
    # =====================================================
    # DASHBOARD ANALYTICS
    # =====================================================

    @action(detail=False, methods=['get'], url_path='dashboard/analytics')
    def dashboard_analytics(self, request):

        date_from = parse_date(request.query_params.get('date_from')) if request.query_params.get('date_from') else None
        date_to = parse_date(request.query_params.get('date_to')) if request.query_params.get('date_to') else None

        events = LoanStateEvent.objects.all()

        if date_from:
            events = events.filter(created_at__date__gte=date_from)
        if date_to:
            events = events.filter(created_at__date__lte=date_to)

        def series(qs):
            return list(
                qs.annotate(date=TruncDate('created_at'))
                .values('date')
                .annotate(value=Count('id'))
                .order_by('date')
            )

        funded_series = FundedPayment.objects.filter(status='completed') \
            .annotate(date=TruncDate('completed_at')) \
            .values('date') \
            .annotate(value=Sum('amount')) \
            .order_by('date')

        collected_series = Payment.objects.filter(status='completed') \
            .annotate(date=TruncDate('processed_at')) \
            .values('date') \
            .annotate(value=Sum('amount')) \
            .order_by('date')

        totals = {
            "funded_payments_amount": str(FundedPayment.objects.aggregate(total=Sum('amount'))['total'] or 0),
            "collected_payments_amount": str(Payment.objects.aggregate(total=Sum('amount'))['total'] or 0),
            "approved_loans_count": events.filter(event_type__in=['ai_approved', 'human_approved']).count(),
            "declined_loans_count": events.filter(event_type__in=['ai_declined', 'human_declined']).count(),
            "funded_loans_count": events.filter(event_type='funded').count(),
            "paid_off_loans_count": events.filter(event_type='paid_off').count(),
            "defaulted_loans_count": events.filter(event_type='defaulted').count(),
            "reactivated_loans_count": events.filter(event_type='reactivated').count(),
            "current_active_loans_count": Loan.objects.filter(is_active=True).count(),
            "current_defaulted_loans_count": Loan.objects.filter(is_active=False).count(),
            "nsf_payments_count": Payment.objects.filter(status='nsf').count(),
            "sent_payments_count": Payment.objects.count(),
            "nsf_ratio": 0,
        }

        return Response({
            "period": {
                "date_from": date_from,
                "date_to": date_to,
            },
            "totals": totals,
            "series": {
                "funded_payments_amount": funded_series,
                "collected_payments_amount": collected_series,
                "approved_loans_count": series(events.filter(event_type__in=['ai_approved', 'human_approved'])),
                "declined_loans_count": series(events.filter(event_type__in=['ai_declined', 'human_declined'])),
                "funded_loans_count": series(events.filter(event_type='funded')),
                "paid_off_loans_count": series(events.filter(event_type='paid_off')),
                "defaulted_loans_count": series(events.filter(event_type='defaulted')),
                "reactivated_loans_count": series(events.filter(event_type='reactivated')),
                "nsf_ratio": [],
            },
        })


# =====================================================
# PAYMENTS
# =====================================================

class PaymentViewSet(viewsets.ModelViewSet):
    queryset = Payment.objects.select_related('loan', 'loan__customer', 'created_by')
    serializer_class = PaymentSerializer
    permission_classes = [StaffOnlyPermission]

    def get_serializer_class(self):
        if self.action == 'create':
            return PaymentCreateSerializer
        return PaymentSerializer

    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        payment = self.get_object()
        payment.complete(user=request.user)
        return Response(PaymentSerializer(payment).data)

    @action(detail=True, methods=['post'])
    def fail(self, request, pk=None):
        payment = self.get_object()
        payment.fail(request.data.get('reason', ''))
        return Response(PaymentSerializer(payment).data)

    @action(detail=True, methods=['post'])
    def nsf(self, request, pk=None):
        payment = self.get_object()
        payment.mark_nsf()
        return Response(PaymentSerializer(payment).data)