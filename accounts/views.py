from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.pagination import PageNumberPagination
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth.models import update_last_login
from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.middleware.csrf import get_token
from django.utils import timezone
from .models import User, Customer, GlobalSetting
from .serializers import (
    UserSerializer, UserCreateSerializer, LoginSerializer,
    CustomerSerializer, CustomerListSerializer, CustomerCreateSerializer,
    CustomerApplySerializer, CustomerPasswordSetupSerializer,
    CustomerPortalLoginSerializer, CustomerPortalMeSerializer,
    CustomerPortalLoanSerializer,
    CustomerPortalDashboardSerializer,
    CustomerJobReferencesSerializer,
    CustomerSignupStartSerializer, CustomerSignupVerifyPhoneSerializer,
    CustomerPortalRequestOTPSerializer, CustomerPortalVerifyOTPSerializer,
    CustomerPasswordResetRequestSerializer,
    CustomerPasswordResetVerifySerializer,
    CustomerPasswordResetConfirmSerializer,
    ApiIntegrationsSerializer,
)


class StaffOnlyPermission(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.user_type == 'staff'
        )


class ApiIntegrationsView(APIView):
    permission_classes = [permissions.IsAuthenticated, StaffOnlyPermission]

    def get(self, request):
        keys = {
            'FLINKS_IFRAME_URL': 'flinks_iframe_url',
            'FLINKS_INSTANCE': 'flinks_instance',
            'FLINKS_CUSTOMER_ID': 'flinks_customer_id',
            'FLINKS_SECRET_KEY_CA': 'flinks_secret_key',
            'ZUMRAILS_API_BASE_URL': 'zum_api_base_url',
            'ZUMRAILS_USERNAME': 'zum_username',
            'ZUMRAILS_PASSWORD': 'zum_password',
            'ZUMRAILS_WALLET_ID': 'zum_wallet_id',
            'ZUMRAILS_FUNDING_SOURCE_ID': 'zum_funding_source_id',
            'ZUMRAILS_WEBHOOK_SECRET': 'zum_webhook_secret',
            'ZUMRAILS_INTERAC_SECURITY_QUESTION': 'zum_interac_security_question',
            'ZUMRAILS_INTERAC_SECURITY_ANSWER': 'zum_interac_security_answer',
            'WEBHOOK_URL': 'webhook_url',
            'ARRIVE_API_KEY': 'arrive_api_key',
            'ARRIVE_WEBHOOK_URL': 'arrive_webhook_url',
            'ARRIVE_WEBHOOK_SECRET': 'arrive_webhook_secret',
            'ARRIVE_PORTAL_BASE_URL': 'arrive_portal_base_url',
            'EMAIL_HOST_USER': 'email',
            'EMAIL_HOST_PASSWORD': 'password',
        }
        
        data = {}
        for db_key, ui_key in keys.items():
            # Fallback to settings if not in DB
            val = GlobalSetting.get_value(db_key)
            if val is None:
                val = getattr(settings, db_key, '')
            data[ui_key] = val

        return Response(data)

    def patch(self, request):
        serializer = ApiIntegrationsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({'message': 'Integrations updated successfully'})
from contracts.models import Contract
from contracts.serializers import (
    ContractSerializer,
    CustomerSignContractSerializer,
)
from loans.models import Loan
from loans.serializers import CurrentApplicationSerializer
from loans.services import LoanService
from .tasks import send_welcome_email





class CustomerPagination(PageNumberPagination):
    page_size = 15
    page_size_query_param = 'page_size'
    max_page_size = 50


def clear_auth_cookies(response):
    response.delete_cookie(settings.AUTH_COOKIE_ACCESS, path=settings.AUTH_COOKIE_ACCESS_PATH)
    response.delete_cookie(settings.AUTH_COOKIE_REFRESH, path=settings.AUTH_COOKIE_REFRESH_PATH)
    return response


def mark_user_login(user):
    if settings.SIMPLE_JWT.get("UPDATE_LAST_LOGIN", False):
        update_last_login(None, user)


def set_csrf_cookie(response, csrf_token: str):
    response.set_cookie(
        key=settings.CSRF_COOKIE_NAME,
        value=csrf_token,
        max_age=settings.CSRF_COOKIE_AGE,
        path=settings.CSRF_COOKIE_PATH,
        domain=settings.CSRF_COOKIE_DOMAIN,
        secure=settings.CSRF_COOKIE_SECURE,
        httponly=settings.CSRF_COOKIE_HTTPONLY,
        samesite=settings.CSRF_COOKIE_SAMESITE,
    )
    return response


def set_auth_cookies(request, response, access_token: str, refresh_token: str):
    csrf_token = get_token(request)

    set_csrf_cookie(response, csrf_token)
    if isinstance(response.data, dict):
        response.data.setdefault("csrf_token", csrf_token)

    response.set_cookie(
        key=settings.AUTH_COOKIE_ACCESS,
        value=access_token,
        httponly=settings.AUTH_COOKIE_HTTP_ONLY,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        path=settings.AUTH_COOKIE_ACCESS_PATH,
        max_age=settings.AUTH_COOKIE_MAX_AGE_ACCESS,
    )
    response.set_cookie(
        key=settings.AUTH_COOKIE_REFRESH,
        value=refresh_token,
        httponly=settings.AUTH_COOKIE_HTTP_ONLY,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        path=settings.AUTH_COOKIE_REFRESH_PATH,
        max_age=settings.AUTH_COOKIE_MAX_AGE_REFRESH,
    )
    return response


class LoginView(APIView):
    permission_classes = [permissions.AllowAny]
    
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        user = serializer.validated_data['user']
        mark_user_login(user)
        refresh = RefreshToken.for_user(user)

        access_token = str(refresh.access_token)
        refresh_token = str(refresh)

        response = Response({
            'user': UserSerializer(user).data
        })

        return set_auth_cookies(request, response, access_token, refresh_token)


class LogoutView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        refresh_token = request.COOKIES.get(settings.AUTH_COOKIE_REFRESH)
        response = Response({'message': 'Successfully logged out'})

        if refresh_token:
            try:
                token = RefreshToken(refresh_token)
                token.blacklist()
            except Exception:
                pass

        return clear_auth_cookies(response)


class CurrentUserView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    
    def get(self, request):
        serializer = UserSerializer(request.user)
        return Response(serializer.data)


class CsrfTokenView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        csrf_token = get_token(request)
        response = Response({"csrf_token": csrf_token})
        return set_csrf_cookie(response, csrf_token)


class RefreshTokenView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    
    def post(self, request):
        refresh_token = request.COOKIES.get(settings.AUTH_COOKIE_REFRESH)
        if not refresh_token and isinstance(request.data, dict):
            refresh_token = request.data.get('refresh')
        if not refresh_token:
            auth_header = request.headers.get('Authorization') or ''
            parts = auth_header.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == 'bearer':
                refresh_token = parts[1].strip()

        if not refresh_token:
            return Response(
                {'error': 'Refresh token required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            serializer = TokenRefreshSerializer(data={'refresh': refresh_token})
            serializer.is_valid(raise_exception=True)

            access_token = serializer.validated_data['access']
            new_refresh_token = serializer.validated_data.get('refresh', refresh_token)

            response = Response({
                'message': 'Token refreshed',
                'access': access_token,
                'refresh': str(new_refresh_token),
            })
            return set_auth_cookies(request, response, access_token, str(new_refresh_token))
        except Exception:
            response = Response(
                {'error': 'Invalid refresh token'},
                status=status.HTTP_401_UNAUTHORIZED
            )
            return clear_auth_cookies(response)


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    permission_classes = [StaffOnlyPermission]
    
    def get_serializer_class(self):
        if self.action == 'create':
            return UserCreateSerializer
        return UserSerializer
    
    def get_queryset(self):
        queryset = User.objects.all()
        
        permission_level = self.request.query_params.get('permission_level')
        if permission_level:
            queryset = queryset.filter(permission_level=permission_level)
        
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')
        
        return queryset
    
    @action(detail=False, methods=['get'])
    def me(self, request):
        serializer = UserSerializer(request.user)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        user = self.get_object()
        user.is_active = True
        user.save(update_fields=['is_active', 'updated_at'])
        return Response({'message': 'User activated'})
    
    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        user = self.get_object()
        user.is_active = False
        user.save(update_fields=['is_active', 'updated_at'])
        return Response({'message': 'User deactivated'})


class CustomerViewSet(viewsets.ModelViewSet):
    permission_classes = [StaffOnlyPermission]
    pagination_class = CustomerPagination

    def get_queryset(self):
        queryset = Customer.objects.annotate(
            loan_count=Count('loans', distinct=True)
        )

        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(first_name__icontains=search) |
                Q(last_name__icontains=search) |
                Q(email__icontains=search) |
                Q(phone__icontains=search)
            )

        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        province = self.request.query_params.get('province')
        if province:
            queryset = queryset.filter(province=province)

        created_from = self.request.query_params.get('created_from')
        if created_from:
            queryset = queryset.filter(created_at__date__gte=created_from)

        created_to = self.request.query_params.get('created_to')
        if created_to:
            queryset = queryset.filter(created_at__date__lte=created_to)

        has_loans = self.request.query_params.get('has_loans')
        if has_loans is not None:
            if has_loans.lower() == 'true':
                queryset = queryset.filter(loan_count__gt=0)
            elif has_loans.lower() == 'false':
                queryset = queryset.filter(loan_count=0)

        allowed_ordering = {
            'created_at': 'created_at',
            '-created_at': '-created_at',
            'updated_at': 'updated_at',
            '-updated_at': '-updated_at',
            'first_name': 'first_name',
            '-first_name': '-first_name',
            'last_name': 'last_name',
            '-last_name': '-last_name',
            'email': 'email',
            '-email': '-email',
        }
        ordering = self.request.query_params.get('ordering', '-created_at')
        queryset = queryset.order_by(allowed_ordering.get(ordering, '-created_at'))

        return queryset

    def get_serializer_class(self):
        if self.action == 'list':
            return CustomerListSerializer
        if self.action == 'create':
            return CustomerCreateSerializer
        return CustomerSerializer

    @action(detail=True, methods=['get'])
    def loans(self, request, pk=None):
        customer = self.get_object()
        from loans.serializers import CustomerLoanDetailSerializer
        loans = (
            customer.loans.select_related('formula')
            .prefetch_related('payments')
            .order_by('-created_at')
        )
        serializer = CustomerLoanDetailSerializer(loans, many=True)
        return Response(serializer.data)


# --- Customer Portal Views ---

class CustomerApplyView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = CustomerApplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        customer = serializer.save()
        mark_user_login(customer.portal_user)

        send_welcome_email.delay(str(customer.id))

        refresh = RefreshToken.for_user(customer.portal_user)
        access_token = str(refresh.access_token)
        refresh_token = str(refresh)

        response = Response({
            'customer': CustomerPortalMeSerializer(customer).data
        }, status=status.HTTP_201_CREATED)

        return set_auth_cookies(request, response, access_token, refresh_token)

class CustomerPasswordSetupView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = CustomerPasswordSetupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({'message': 'Password set successfully'})


class CustomerSignupStartView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = CustomerSignupStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = serializer.save()

        if result["existing_account"]:
            return Response({
                "action": "existing_customer",
                "existing_account": True,
                "challenge_id": None,
                "message": "Welcome back. Continue to your customer portal.",
            }, status=status.HTTP_200_OK)

        return Response({
            "action": "verify_phone",
            "existing_account": False,
            "challenge_id": result["challenge_id"],
            "message": "Verification code sent.",
        }, status=status.HTTP_200_OK)


class CustomerSignupVerifyPhoneView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = CustomerSignupVerifyPhoneSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        customer = serializer.save()
        mark_user_login(customer.portal_user)

        send_welcome_email.delay(str(customer.id))

        refresh = RefreshToken.for_user(customer.portal_user)

        response = Response({
            'customer': CustomerPortalMeSerializer(customer).data,
        }, status=status.HTTP_201_CREATED)

        return set_auth_cookies(request, response, str(refresh.access_token), str(refresh))


class CustomerPasswordResetRequestView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = CustomerPasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = serializer.save()

        return Response(result)


class CustomerPasswordResetVerifyView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = CustomerPasswordResetVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        return Response({'message': 'OTP verified'})


class CustomerPasswordResetConfirmView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = CustomerPasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response({'message': 'Password reset successful'})


class CustomerPortalRequestOTPView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = CustomerPortalRequestOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = serializer.save()

        return Response({
            "challenge_id": result["challenge_id"],
            "delivery": result["delivery"],
            "message": "Verification code sent.",
        }, status=status.HTTP_200_OK)


class CustomerPortalVerifyOTPView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = CustomerPortalVerifyOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user, customer = serializer.save()
        mark_user_login(user)

        refresh = RefreshToken.for_user(user)

        response = Response({
            "customer": CustomerPortalMeSerializer(customer).data,
        }, status=status.HTTP_200_OK)

        return set_auth_cookies(request, response, str(refresh.access_token), str(refresh))


class CustomerPortalLoginView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = CustomerPortalLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        user = serializer.validated_data['user']
        mark_user_login(user)
        refresh = RefreshToken.for_user(user)

        access_token = str(refresh.access_token)
        refresh_token = str(refresh)

        response = Response({
            'customer': CustomerPortalMeSerializer(serializer.validated_data['customer']).data
        })

        return set_auth_cookies(request, response, access_token, refresh_token)


class CustomerPortalBaseView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    # In-progress applications must outrank terminal states so a re-application
    # is visible while a previous declined loan still exists.
    APPLICATION_PRIORITY = [
        'pending_signature',
        'ibv_pending',
        'pending',
        'pending_funding',
        'active',
        'defaulted',
        'human_declined',
        'paid_off',
    ]

    def get_customer(self, request):
        if request.user.user_type != 'customer':
            return None, Response(
                {'error': 'Customer portal access only'},
                status=status.HTTP_403_FORBIDDEN
            )

        try:
            customer = request.user.customer_profile
            return customer, None
        except Customer.DoesNotExist:
            return None, Response(
                {'error': 'Customer profile not found'},
                status=status.HTTP_404_NOT_FOUND
            )

    def get_current_application(self, customer):
        for status_value in self.APPLICATION_PRIORITY:
            loan = customer.loans.filter(status=status_value).order_by('-created_at').first()
            if loan:
                return loan
        return None


class CustomerPortalMeView(CustomerPortalBaseView):
    def get(self, request):
        customer, error_response = self.get_customer(request)
        if error_response:
            return error_response

        serializer = CustomerPortalMeSerializer(customer)
        return Response(serializer.data)


class CustomerPortalFlinksConfigView(CustomerPortalBaseView):
    def get(self, request):
        customer, error_response = self.get_customer(request)
        if error_response:
            return error_response

        flinks_iframe_url = GlobalSetting.get_value(
            'FLINKS_IFRAME_URL',
            getattr(settings, 'FLINKS_IFRAME_URL', ''),
        )

        return Response({
            'flinks_iframe_url': flinks_iframe_url or '',
        })


class CustomerPortalMyLoansView(CustomerPortalBaseView):
    def get(self, request):
        customer, error_response = self.get_customer(request)
        if error_response:
            return error_response

        from loans.serializers import CustomerLoanDetailSerializer

        loans = (
            customer.loans.select_related('formula')
            .prefetch_related('payments')
            .order_by('-created_at')
        )
        serializer = CustomerLoanDetailSerializer(loans, many=True)

        return Response(serializer.data)


class CustomerPortalCurrentApplicationView(CustomerPortalBaseView):
    """
    Return the customer's most relevant current loan/application.
    """

    def get(self, request):
        customer, error_response = self.get_customer(request)

        if error_response:
            return error_response

        selected_loan = self.get_current_application(customer)

        if not selected_loan:
            return Response(
                {'detail': 'No application found.'},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = CurrentApplicationSerializer(selected_loan)

        return Response(serializer.data)


class CustomerPortalDashboardView(CustomerPortalBaseView):
    """
    Single source of truth for customer portal routing/state.
    """

    def get(self, request):
        customer, error_response = self.get_customer(request)
        if error_response:
            return error_response

        from loans.services import LoanService

        loan = self.get_current_application(customer)
        connection = (
            customer.bank_connections.filter(is_active=True)
            .order_by('-created_at')
            .first()
        )

        banking = {
            'verified': customer.banking_verified,
            'has_connection': connection is not None,
            'connection_status': connection.sync_status if connection else None,
            'last_synced_at': connection.last_synced_at if connection else None,
        }

        current_application = None
        if loan:
            current_application = CurrentApplicationSerializer(loan).data

        portal_state = 'no_application'
        next_step = 'apply'
        next_url = '/apply'

        can_appeal = False
        can_renew = False
        can_refinance = False
        can_start_new_application = LoanService.can_start_new_application(customer)

        if not loan:
            portal_state = 'no_application'
            next_step = 'apply'
            next_url = '/apply'

        elif loan.status == 'human_declined':
            portal_state = 'declined'
            next_step = 'start_new_application' if can_start_new_application else 'appeal'
            next_url = '/customer/loans'
            can_appeal = True

        elif loan.status == 'paid_off':
            portal_state = 'paid_off'
            next_step = 'renewal'
            next_url = '/customer/loans'
            can_renew = True

        elif loan.status in ['active', 'defaulted']:
            portal_state = 'active_loan'
            next_step = 'loans'
            next_url = '/customer/loans'

        elif not customer.banking_verified:
            if connection and connection.sync_status in ['pending', 'syncing']:
                portal_state = 'banking_processing'
                next_step = 'banking'
                next_url = '/customer/banking'
            elif connection and connection.sync_status == 'failed':
                portal_state = 'banking_failed'
                next_step = 'banking'
                next_url = '/customer/banking'
            else:
                portal_state = 'awaiting_banking'
                next_step = 'banking'
                next_url = '/customer/banking'

        elif loan.status == 'pending_signature':
            portal_state = 'contract_required'
            next_step = 'contract'
            next_url = '/customer/contracts'

        elif loan.status == 'ibv_pending':
            portal_state = 'contract_required'
            next_step = 'contract'
            next_url = '/customer/contracts'

        elif loan.status == 'pending_funding':
            if not customer.contract_completed:
                portal_state = 'contract_required'
                next_step = 'contract'
                next_url = '/customer/contracts'
            else:
                portal_state = 'pending_funding'
                next_step = 'funding'
                next_url = '/customer/loans'

        elif loan.status == 'pending':
            if not customer.contract_completed:
                portal_state = 'contract_required'
                next_step = 'contract'
                next_url = '/customer/contracts'
            else:
                portal_state = 'manual_review'
                next_step = 'review'
                next_url = '/customer/loans'

        payload = {
            'customer': CustomerPortalMeSerializer(customer).data,
            'current_application': current_application,
            'portal_state': portal_state,
            'next_step': next_step,
            'next_url': next_url,
            'can_appeal': can_appeal,
            'can_renew': can_renew,
            'can_refinance': can_refinance,
            'can_start_new_application': can_start_new_application,
            'banking': banking,
        }

        serializer = CustomerPortalDashboardSerializer(payload)
        return Response(serializer.data)


class CustomerPortalStartNewApplicationView(CustomerPortalBaseView):
    """
    Declined customers only: keep prior loans and open a fresh IBV + contract path.
    """

    def post(self, request):
        customer, error_response = self.get_customer(request)
        if error_response:
            return error_response

        from loans.services import LoanService

        try:
            loan = LoanService.start_new_application(customer)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        customer.refresh_from_db()
        return Response(
            {
                'message': 'New application started. Please complete banking verification.',
                'loan_id': str(loan.id),
                'next_url': '/customer/banking',
                'current_application': CurrentApplicationSerializer(loan).data,
                'can_start_new_application': LoanService.can_start_new_application(customer),
            },
            status=status.HTTP_200_OK,
        )


class CustomerPortalContractPreviewView(CustomerPortalBaseView):
    """
    Return/create the current loan agreement (rendered with live variables).
    """

    def get(self, request):
        customer, error_response = self.get_customer(request)

        if error_response:
            return error_response

        if not customer.banking_verified:
            return Response(
                {'error': 'Banking verification must be completed before accessing the contract.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        loan = customer.loans.filter(
            status__in=[
                'pending_signature',
                'ibv_pending',
                'pending',
                'pending_funding',
            ]
        ).order_by('-created_at').first()

        if not loan:
            signed_contract = Contract.objects.filter(
                customer=customer,
                status='signed',
            ).order_by('-signed_date').first()
            if signed_contract:
                serializer = ContractSerializer(signed_contract)
                return Response(serializer.data)

            return Response(
                {'error': 'No application available.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        from contracts.services import AGREEMENT_VERSION, render_loan_agreement
        if loan.status == 'ibv_pending':
            loan = LoanService.mark_pending_signature(loan)

        contract = (
            Contract.objects.filter(customer=customer, loan=loan, status='signed')
            .order_by('-signed_date', '-created_at')
            .first()
            or Contract.objects.filter(customer=customer, loan=loan)
            .order_by('-created_at')
            .first()
        )
        if not contract:
            contract = Contract.objects.create(
                customer=customer,
                loan=loan,
                created_by=None,
                agreement_version=AGREEMENT_VERSION,
                agreement_text=render_loan_agreement(customer, loan),
            )

        # Keep draft agreements fresh with current loan/bank variables.
        if contract.status != 'signed':
            rendered = render_loan_agreement(customer, loan)
            if (
                contract.agreement_text != rendered
                or contract.agreement_version != AGREEMENT_VERSION
            ):
                contract.agreement_text = rendered
                contract.agreement_version = AGREEMENT_VERSION
                contract.save(
                    update_fields=['agreement_text', 'agreement_version', 'updated_at']
                )

        serializer = ContractSerializer(contract)

        return Response(serializer.data)


class CustomerPortalSignContractView(CustomerPortalBaseView):
    """
    Customer signs demonstrative agreement.
    """

    def post(self, request):
        customer, error_response = self.get_customer(request)

        if error_response:
            return error_response

        if not customer.banking_verified:
            return Response(
                {'error': 'Banking verification must be completed before signing the contract.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = CustomerSignContractSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        loan = customer.loans.filter(
            status__in=[
                'pending_signature',
                'ibv_pending',
                'pending',
                'pending_funding',
            ]
        ).order_by('-created_at').first()

        if not loan:
            return Response(
                {'error': 'No application available.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        from contracts.services import AGREEMENT_VERSION, render_loan_agreement
        if loan.status == 'ibv_pending':
            loan = LoanService.mark_pending_signature(loan)

        contract = (
            Contract.objects.filter(customer=customer, loan=loan)
            .exclude(status='signed')
            .order_by('-created_at')
            .first()
            or Contract.objects.filter(customer=customer, loan=loan, status='signed')
            .order_by('-signed_date', '-created_at')
            .first()
        )
        if not contract:
            contract = Contract.objects.create(
                customer=customer,
                loan=loan,
                agreement_version=AGREEMENT_VERSION,
                agreement_text=render_loan_agreement(customer, loan),
            )

        # Freeze the filled agreement text at signature time.
        if contract.status != 'signed':
            contract.agreement_text = render_loan_agreement(customer, loan)
            contract.agreement_version = AGREEMENT_VERSION

        contract.typed_name = serializer.validated_data['typed_name']
        contract.signer_email = customer.email
        contract.signer_ip = request.META.get('REMOTE_ADDR')

        contract.accepted_terms = serializer.validated_data['accepted_terms']
        contract.accepted_credit_check = serializer.validated_data['accepted_credit_check']
        contract.accepted_banking_review = serializer.validated_data['accepted_banking_review']
        contract.accepted_electronic_signature = serializer.validated_data['accepted_electronic_signature']

        contract.status = 'signed'
        contract.signed_date = timezone.now()

        contract.save()

        loan = LoanService.sign_customer_contract(customer)

        template_name = 'We Have Received Your Request Template'
        from communications.models import CommunicationTemplate
        from communications.tasks import send_template_message

        template = CommunicationTemplate.objects.filter(
            name=template_name,
            type='email',
            is_active=True,
        ).first()
        if template and not loan.communications.filter(
            direction='outbound',
            type='email',
            template_name=template_name,
        ).exists():
            customer_id = str(customer.id)
            loan_id = str(loan.id)
            template_id = str(template.id)
            transaction.on_commit(
                lambda: send_template_message.delay(customer_id, template_id, loan_id)
            )

        return Response({
            'message': 'Agreement signed successfully.',
            'contract': ContractSerializer(contract).data,
            'show_job_references_prompt': True,
        })


class CustomerPortalJobReferencesView(CustomerPortalBaseView):
    """
    Optional job and reference details collected after contract signing.
    """

    def get(self, request):
        customer, error_response = self.get_customer(request)
        if error_response:
            return error_response

        return Response({
            'job_place_name': customer.job_place_name or '',
            'supervisor_name': customer.supervisor_name or '',
            'supervisor_phone': customer.supervisor_phone or '',
            'reference_1_name': customer.reference_1_name or '',
            'reference_1_phone': customer.reference_1_phone or '',
            'reference_2_name': customer.reference_2_name or '',
            'reference_2_phone': customer.reference_2_phone or '',
            'references_completed': customer.references_completed,
        })

    def patch(self, request):
        customer, error_response = self.get_customer(request)
        if error_response:
            return error_response
        # If customer has already completed references, prevent further updates
        if customer.references_completed:
            return Response(
                {"error": "Job references have already been submitted."},
                status=status.HTTP_403_FORBIDDEN
            )

        serializer = CustomerJobReferencesSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        customer = serializer.save(customer)

        return Response({
            'message': 'Job and reference information updated.',
            'customer': CustomerPortalMeSerializer(customer).data,
        })


class CustomerPortalRunAnalysisView(CustomerPortalBaseView):
    """
    Temporary mock AI analysis.
    """

    def post(self, request):
        customer, error_response = self.get_customer(request)

        if error_response:
            return error_response

        loan = LoanService.run_mock_ai_analysis(customer)

        return Response({
            'message': 'Analysis completed.',
            'status': loan.status,
            'loan': CurrentApplicationSerializer(loan).data,
        })
