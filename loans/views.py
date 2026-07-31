from decimal import Decimal
import re

from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response

from django.utils import timezone
from django.utils.dateparse import parse_date
from django.db.models import Q, Sum, Count, F, Value, CharField
from django.db.models.functions import TruncDate, Concat

from banking.models import BankAccount

from .models import (
    CollectionPayment,
    FundingMethodRecommendation,
    Loan,
    Payment,
    LoanStateEvent,
    FundedPayment,
    LoanFormula,
)
from .services import LoanService
from .zumrails import (
    CollectionService,
    FundingConfigurationService,
    FundingService,
    SettlementService,
    ZumRailsRequestError,
    funding_configuration_ready,
)
from .serializers import (
    CollectionInitiateSerializer,
    CollectionPaymentSerializer,
    CollectionsAccountChangeAuditSerializer,
    CollectionsAccountUpdateSerializer,
    FundingMethodRecommendationSerializer,
    LoanFormulaSerializer,
    LoanSerializer,
    LoanListSerializer,
    LoanCreateSerializer,
    PaymentSerializer,
    PaymentCreateSerializer,
    LoanApproveSerializer,
    LoanAmountUpdateSerializer,
    LoanDeclineSerializer,
    LoanFundSerializer,
    LoanFundingConfigurationSerializer,
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


class ManagerOrAdminPermission(permissions.BasePermission):
    """Staff Manager (4) or Admin (5) only."""

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and getattr(user, 'user_type', None) == 'staff'
            and getattr(user, 'has_permission', lambda _lvl: False)(4)
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


class FundingMethodRecommendationViewSet(viewsets.ModelViewSet):
    queryset = FundingMethodRecommendation.objects.all()
    serializer_class = FundingMethodRecommendationSerializer
    permission_classes = [StaffOnlyPermission]

    def get_queryset(self):
        qs = super().get_queryset()
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() == 'true')
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
    def _filtered_queryset(self, *, ignore_status=False):
        qs = super().get_queryset()

        status_param = None if ignore_status else self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)

        ai_decision_param = self.request.query_params.get('ai_decision')
        if ai_decision_param:
            qs = qs.filter(ai_decision=ai_decision_param)

        ibv_status_param = self.request.query_params.get('ibv_status')
        if ibv_status_param == 'pending':
            qs = qs.filter(Q(status='ibv_pending') | Q(customer__banking_verified=False))
        elif ibv_status_param == 'completed':
            qs = qs.filter(customer__banking_verified=True)

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
            raw = search.strip()
            if raw:
                qs = qs.annotate(
                    _customer_full_name=Concat(
                        'customer__first_name',
                        Value(' '),
                        'customer__last_name',
                        output_field=CharField(),
                    )
                )
                filters = (
                    Q(id__icontains=raw) |
                    Q(customer__first_name__icontains=raw) |
                    Q(customer__last_name__icontains=raw) |
                    Q(customer__email__icontains=raw) |
                    Q(customer__phone__icontains=raw) |
                    Q(customer__phone_normalized__icontains=raw) |
                    Q(_customer_full_name__icontains=raw)
                )

                # Multi-word English names: each token can match first or last name.
                tokens = [token for token in raw.split() if token]
                if len(tokens) >= 2:
                    name_q = Q()
                    for token in tokens:
                        name_q &= (
                            Q(customer__first_name__icontains=token) |
                            Q(customer__last_name__icontains=token) |
                            Q(_customer_full_name__icontains=token)
                        )
                    filters |= name_q

                # Phone search ignores formatting: "(416) 555-0100" → "4165550100".
                digits = re.sub(r'\D', '', raw)
                if len(digits) >= 3:
                    filters |= (
                        Q(customer__phone__icontains=digits) |
                        Q(customer__phone_normalized__icontains=digits)
                    )

                qs = qs.filter(filters)

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

    def get_queryset(self):
        return self._filtered_queryset(ignore_status=False)

    @action(detail=False, methods=['get'], url_path='status-summary')
    def status_summary(self, request):
        """
        Full loan counts by status bucket for the current list filters.
        Ignores `status` so top cards stay a useful breakdown while other
        filters (search, province, AI, IBV, dates) still apply.
        """
        qs = self._filtered_queryset(ignore_status=True)
        by_status = {
            row['status']: row['count']
            for row in qs.values('status').annotate(count=Count('id'))
        }

        pending_statuses = ('ibv_pending', 'pending', 'pending_signature')
        return Response({
            'pending': sum(by_status.get(status, 0) for status in pending_statuses),
            'approved': by_status.get('pending_funding', 0),
            'active': by_status.get('active', 0),
            'declined': by_status.get('human_declined', 0),
            'paid_off': by_status.get('paid_off', 0),
            'defaulted': by_status.get('defaulted', 0),
        })

    # =====================================================
    # ACTIONS
    # =====================================================

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        loan = self.get_object()

        if loan.status not in ['pending', 'pending_signature']:
            return Response({'error': 'Only pending or pending signature loans can be approved'}, status=400)

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

    @action(detail=True, methods=['patch'], url_path='approved-amount')
    def update_approved_amount(self, request, pk=None):
        loan = self.get_object()

        serializer = LoanAmountUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            loan = LoanService.update_approved_amount(
                loan=loan,
                principal=serializer.validated_data['principal'],
                user=request.user,
                notes=serializer.validated_data.get('notes') or '',
            )
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)

        return Response(LoanSerializer(loan).data)

    @action(detail=True, methods=['post'])
    def decline(self, request, pk=None):
        loan = self.get_object()

        if loan.status not in ['ibv_pending', 'pending', 'pending_signature', 'pending_funding']:
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

    @action(
        detail=True,
        methods=['post'],
        url_path='revert-decline',
        permission_classes=[ManagerOrAdminPermission],
    )
    def revert_decline(self, request, pk=None):
        loan = self.get_object()

        if loan.status != 'human_declined':
            return Response(
                {'error': 'Only human-declined loans can be reverted to approve'},
                status=400,
            )

        notes = ''
        if isinstance(request.data, dict):
            notes = (request.data.get('notes') or '').strip()

        try:
            loan = LoanService.revert_decline_to_approve(
                loan=loan,
                approved_by=request.user,
                notes=notes or None,
            )
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)

        return Response(LoanSerializer(loan).data)

    @action(detail=True, methods=['post'])
    def fund(self, request, pk=None):
        if not ManagerOrAdminPermission().has_permission(request, self):
            return Response(
                {'error': 'Only Manager or Admin can fund loans.'},
                status=403,
            )

        loan = self.get_object()

        from loans.zumrails import is_arrive_funded_loan
        serializer = LoanFundSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        method = serializer.validated_data['method']

        if is_arrive_funded_loan(loan) and method in ('eft', 'etransfer'):
            return Response(
                {
                    'error': (
                        'Arrive loans cannot be funded via EFT / e-Transfer. '
                        'Use Card Issuance.'
                    )
                },
                status=400,
            )

        if loan.status != 'pending_funding':
            return Response({'error': 'Only signed/approved loans can be funded'}, status=400)
        if not loan.contract_signed:
            return Response({'error': 'Contract must be signed before funding.'}, status=400)

        recommended_method = FundingMethodRecommendation.for_date()
        if is_arrive_funded_loan(loan):
            recommended_method = 'card_issuance'
        elif recommended_method == 'card_issuance':
            recommended_method = 'eft'
        selected_method = method
        if recommended_method and recommended_method != selected_method:
            if not serializer.validated_data.get('override_confirmed'):
                return Response({'error': 'Override confirmation required.'}, status=400)

        try:
            collections_account = None
            collections_account_id = serializer.validated_data.get('collections_account_id')
            if collections_account_id:
                collections_account = BankAccount.objects.get(
                    id=collections_account_id,
                    customer=loan.customer,
                )

            funded_payment = FundingService.initiate(
                loan=loan,
                method=selected_method,
                schedule_confirmed=serializer.validated_data['schedule_confirmed'],
                user=request.user,
                destination=serializer.validated_data.get('funding_destination') or None,
                collections_account=collections_account,
            )
        except BankAccount.DoesNotExist:
            return Response({'error': 'Collections account required'}, status=400)
        except ZumRailsRequestError as exc:
            return Response({'error': str(exc)}, status=502)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)

        loan.refresh_from_db()
        return Response({
            'loan': LoanSerializer(loan).data,
            'funded_payment': FundedPaymentSerializer(funded_payment).data,
        })

    @action(detail=True, methods=['post'], url_path='funding/initiate')
    def initiate_funding(self, request, pk=None):
        return self.fund(request, pk=pk)

    @action(detail=True, methods=['get'], url_path='funding/options')
    def funding_options(self, request, pk=None):
        loan = self.get_object()
        from loans.zumrails import is_arrive_funded_loan

        recommended_method = FundingMethodRecommendation.for_date()
        if is_arrive_funded_loan(loan):
            recommended_method = 'card_issuance'
        elif recommended_method == 'card_issuance':
            recommended_method = 'eft'
        collections_account = loan.collections_account or loan.bank_account
        readiness = funding_configuration_ready(loan)
        return Response({
            'amount': loan.principal,
            'recommended_method': recommended_method,
            'selected_method': loan.funding_method,
            'funding_destination': loan.funding_destination,
            'collections_account': collections_account.id if collections_account else None,
            'schedule_confirmed': False,
            **readiness,
        })

    @action(detail=True, methods=['patch'], url_path='funding/configuration')
    def configure_funding(self, request, pk=None):
        loan = self.get_object()

        from loans.zumrails import is_arrive_funded_loan
        serializer = LoanFundingConfigurationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            # Arrive: only collections account is configurable (Card Issuance funds the card).
            if is_arrive_funded_loan(loan):
                loan = FundingConfigurationService.configure(
                    loan,
                    collections_account_id=serializer.validated_data.get('collections_account_id'),
                    user=request.user,
                )
            else:
                loan = FundingConfigurationService.configure(
                    loan,
                    emt_email=serializer.validated_data.get('emt_email'),
                    emt_source=serializer.validated_data.get('emt_source'),
                    eft_bank_account_id=serializer.validated_data.get('eft_bank_account_id'),
                    collections_account_id=serializer.validated_data.get('collections_account_id'),
                    user=request.user,
                )
        except BankAccount.DoesNotExist:
            return Response({'error': 'Selected account must belong to this customer.'}, status=400)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)

        readiness = funding_configuration_ready(loan)
        collections_account = loan.collections_account or loan.bank_account
        return Response({
            'loan_id': str(loan.id),
            'funding_destination': loan.funding_destination,
            'collections_account': collections_account.id if collections_account else None,
            **readiness,
        })

    @action(detail=True, methods=['post'], url_path='collections/initiate')
    def initiate_collection(self, request, pk=None):
        loan = self.get_object()

        serializer = CollectionInitiateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        payment = None
        payment_id = serializer.validated_data.get('payment_id')
        if payment_id:
            payment = Payment.objects.filter(id=payment_id, loan=loan).first()
            if not payment:
                return Response({'error': 'Payment not found for this loan'}, status=404)

        try:
            collection = CollectionService.initiate(
                loan=loan,
                amount=serializer.validated_data['amount'],
                payment=payment,
                user=request.user,
            )
        except ZumRailsRequestError as exc:
            return Response({'error': str(exc)}, status=502)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)

        return Response(CollectionPaymentSerializer(collection).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch'], url_path='collections-account')
    def update_collections_account(self, request, pk=None):
        loan = self.get_object()
        serializer = CollectionsAccountUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            account = BankAccount.objects.get(
                id=serializer.validated_data['bank_account_id'],
                customer=loan.customer,
            )
        except BankAccount.DoesNotExist:
            return Response({'error': 'Collections account must belong to this customer.'}, status=400)

        failed_payment_id = serializer.validated_data['failed_payment_id']
        failed_payment = CollectionPayment.objects.filter(id=failed_payment_id, loan=loan).first()
        if not failed_payment:
            return Response({'error': 'Failed payment not found for this loan'}, status=404)

        try:
            audit = CollectionService.change_account(
                loan=loan,
                new_account=account,
                failed_payment=failed_payment,
                user=request.user,
            )
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)

        return Response(CollectionsAccountChangeAuditSerializer(audit).data)

    @action(detail=True, methods=['get'], url_path='collection-payments')
    def collection_payments(self, request, pk=None):
        loan = self.get_object()
        payments = loan.collection_payments.all().order_by('-initiated_at', '-created_at')
        return Response(CollectionPaymentSerializer(payments, many=True).data)

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

    @action(detail=False, methods=['post'], url_path='settlement/process')
    def process_settlement(self, request):
        completed = SettlementService.process_due()
        return Response({'completed': completed})
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

        funded_payments = FundedPayment.objects.all()
        collected_payments = Payment.objects.all()
        nsf_payments = Payment.objects.filter(
            status='nsf',
            processed_at__isnull=False,
        )
        sent_payments = Payment.objects.all()

        if date_from:
            funded_payments = funded_payments.filter(initiated_at__date__gte=date_from)
            collected_payments = collected_payments.filter(scheduled_date__gte=date_from)
            nsf_payments = nsf_payments.filter(processed_at__date__gte=date_from)
            sent_payments = sent_payments.filter(created_at__date__gte=date_from)
        if date_to:
            funded_payments = funded_payments.filter(initiated_at__date__lte=date_to)
            collected_payments = collected_payments.filter(scheduled_date__lte=date_to)
            nsf_payments = nsf_payments.filter(processed_at__date__lte=date_to)
            sent_payments = sent_payments.filter(created_at__date__lte=date_to)

        sent_payments_count = sent_payments.count()
        nsf_payments_count = nsf_payments.count()
        nsf_ratio = round((nsf_payments_count / sent_payments_count) * 100, 2) if sent_payments_count else 0

        funded_series = funded_payments \
            .annotate(date=TruncDate('initiated_at')) \
            .values('date') \
            .annotate(value=Sum('amount')) \
            .order_by('date')

        collected_series = collected_payments \
            .values(date=F('scheduled_date')) \
            .annotate(value=Sum('amount')) \
            .order_by('date')

        totals = {
            "funded_payments_amount": str(funded_payments.aggregate(total=Sum('amount'))['total'] or 0),
            "collected_payments_amount": str(collected_payments.aggregate(total=Sum('amount'))['total'] or 0),
            "approved_loans_count": events.filter(event_type='human_approved').count(),
            "declined_loans_count": events.filter(event_type='human_declined').count(),
            "funded_loans_count": events.filter(event_type='funded').count(),
            "paid_off_loans_count": events.filter(event_type='paid_off').count(),
            "defaulted_loans_count": events.filter(event_type='defaulted').count(),
            "reactivated_loans_count": events.filter(event_type='reactivated').count(),
            "current_active_loans_count": Loan.objects.filter(is_active=True).count(),
            "current_defaulted_loans_count": Loan.objects.filter(is_active=False).count(),
            "nsf_payments_count": nsf_payments_count,
            "sent_payments_count": sent_payments_count,
            "nsf_ratio": nsf_ratio,
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
                "approved_loans_count": series(events.filter(event_type='human_approved')),
                "declined_loans_count": series(events.filter(event_type='human_declined')),
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
