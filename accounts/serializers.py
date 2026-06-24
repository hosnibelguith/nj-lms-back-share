from rest_framework import serializers
from django.contrib.auth import authenticate
from django.db import transaction, IntegrityError
from django.db.models import Q
from .models import User, Customer, AuthOTPChallenge, GlobalSetting
from .utils.phone import normalize_ca_phone
from .services.otp import create_otp_challenge, verify_otp_challenge
from .tasks import send_sms_otp_task, send_email_otp_task

class UserSerializer(serializers.ModelSerializer):
    permission_level_display = serializers.CharField(source='get_permission_level_display', read_only=True)
    
    class Meta:
        model = User
        fields = [
            'id', 'email', 'full_name', 'phone',
            'permission_level', 'permission_level_display',
            'user_type',
            'is_active', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class UserCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)
    
    class Meta:
        model = User
        fields = ['id', 'email', 'full_name', 'phone', 'password', 'permission_level', 'user_type']
        read_only_fields = ['id']
    
    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    
    def validate(self, attrs):
        email = attrs.get('email')
        password = attrs.get('password')
        
        user = authenticate(email=email, password=password)
        
        if not user:
            raise serializers.ValidationError('Invalid email or password')

        if user.user_type != 'staff':
            raise serializers.ValidationError('Use the customer portal login')
        
        if not user.is_active:
            raise serializers.ValidationError('User account is disabled')
        
        attrs['user'] = user
        return attrs


class CustomerSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    full_address = serializers.CharField(read_only=True)
    province_display = serializers.CharField(source='get_province_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    loan_count = serializers.IntegerField(read_only=True)
    onboarding_stage_display = serializers.CharField(source='get_onboarding_stage_display', read_only=True)
    flinks_email = serializers.SerializerMethodField()
    flinks_name = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = [
            'id',
            'first_name',
            'last_name',
            'full_name',
            'email',
            'phone',
            'flinks_email',
            'flinks_name',
            'date_of_birth',
            'address_line_1',
            'address_line_2',
            'city',
            'province',
            'province_display',
            'postal_code',
            'country',
            'full_address',
            'status',
            'status_display',
            'onboarding_stage',
            'onboarding_stage_display',
            'banking_verified',
            'references_completed',
            'contract_completed',
            'created_at',
            'updated_at',
            'loan_count',
        ]
        read_only_fields = [
            'id',
            'full_name',
            'full_address',
            'flinks_email',
            'flinks_name',
            'created_at',
            'updated_at',
            'loan_count',
        ]

    def get_flinks_email(self, obj):
        if obj.portal_user:
            return obj.portal_user.flinks_email or ''
        return ''

    def get_flinks_name(self, obj):
        if obj.portal_user:
            return obj.portal_user.flinks_name or ''
        return ''


class CustomerListSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    loan_count = serializers.IntegerField(read_only=True)
    province_display = serializers.CharField(source='get_province_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = Customer
        fields = [
            'id',
            'first_name',
            'last_name',
            'full_name',
            'email',
            'phone',
            'province',
            'province_display',
            'status',
            'status_display',
            'onboarding_stage',
            'created_at',
            'updated_at',
            'loan_count',
        ]


class CustomerCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = [
            'first_name', 'last_name', 'email', 'phone', 'date_of_birth',
            'address_line_1', 'address_line_2', 'city', 'province', 'postal_code'
        ]
    
    def validate_email(self, value):
        if Customer.objects.filter(email=value).exists():
            raise serializers.ValidationError('A customer with this email already exists')
        return value

class CustomerApplySerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=20)
    province = serializers.ChoiceField(choices=Customer.PROVINCE_CHOICES)
    date_of_birth = serializers.DateField()
    requested_loan_amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
    )

    password = serializers.CharField(write_only=True, min_length=8)
    confirm_password = serializers.CharField(write_only=True)

    def validate_email(self, value):
        if Customer.objects.filter(email=value).exists():
            raise serializers.ValidationError('A customer with this email already exists')
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError('An account with this email already exists')
        return value

    def validate(self, attrs):
        if attrs['password'] != attrs['confirm_password']:
            raise serializers.ValidationError('Passwords do not match')
        return attrs

    def create(self, validated_data):
        from django.utils import timezone

        password = validated_data.pop('password')
        validated_data.pop('confirm_password')

        full_name = f"{validated_data['first_name']} {validated_data['last_name']}".strip()

        portal_user = User.objects.create_user(
            email=validated_data['email'],
            password=password,
            full_name=full_name,
            phone=validated_data['phone'],
            permission_level=1,
            user_type='customer',
            is_staff=False,
            is_superuser=False,
            is_active=True,
        )

        customer = Customer.objects.create(
            portal_user=portal_user,
            first_name=validated_data['first_name'],
            last_name=validated_data['last_name'],
            email=validated_data['email'],
            phone=validated_data['phone'],
            province=validated_data['province'],
            date_of_birth=validated_data['date_of_birth'],
            requested_loan_amount=validated_data['requested_loan_amount'],
            onboarding_stage='banking_verification',
            status='pending',
            phone_verified=True,
            phone_verified_at=timezone.now(),
            references_completed=False,
        )

        from loans.services import LoanService
        LoanService.create_initial_application(customer)
        return customer


class CustomerSignupStartSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=20)
    province = serializers.ChoiceField(choices=Customer.PROVINCE_CHOICES)
    date_of_birth = serializers.DateField()
    requested_loan_amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
    )

    password = serializers.CharField(write_only=True, min_length=8)
    confirm_password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        if attrs['password'] != attrs['confirm_password']:
            raise serializers.ValidationError('Passwords do not match')

        email = User.objects.normalize_email(attrs['email']).lower()
        phone_normalized = normalize_ca_phone(attrs['phone'])

        attrs['email'] = email
        attrs['phone_normalized'] = phone_normalized

        existing_customer = Customer.objects.select_related('portal_user').filter(
            Q(email__iexact=email) | Q(phone_normalized=phone_normalized)
        ).first()

        if existing_customer:
            if (
                existing_customer.portal_user
                and existing_customer.portal_user.user_type == 'customer'
                and existing_customer.portal_user.is_active
            ):
                attrs['existing_account'] = True
                return attrs

            raise serializers.ValidationError(
                'A customer profile already exists for this email or phone. Please contact support.'
            )

        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError(
                'This email cannot be used for a customer application.'
            )

        attrs['existing_account'] = False
        return attrs

    def save(self):
        if self.validated_data['existing_account']:
            return {
                'existing_account': True,
                'challenge_id': None,
            }

        metadata = {
            'first_name': self.validated_data['first_name'],
            'last_name': self.validated_data['last_name'],
            'email': self.validated_data['email'],
            'phone': self.validated_data['phone'],
            'phone_normalized': self.validated_data['phone_normalized'],
            'province': self.validated_data['province'],
            'date_of_birth': self.validated_data['date_of_birth'].isoformat(),
            'requested_loan_amount': str(self.validated_data['requested_loan_amount']),
            'password': self.validated_data['password'],
        }

        challenge, code, should_send = create_otp_challenge(
            identifier=self.validated_data['phone_normalized'],
            purpose=AuthOTPChallenge.PURPOSE_SIGNUP_PHONE,
            metadata=metadata,
        )

        if should_send:
            send_sms_otp_task.delay(self.validated_data['phone_normalized'], code)

        return {
            'existing_account': False,
            'challenge_id': challenge.id,
        }


class CustomerSignupVerifyPhoneSerializer(serializers.Serializer):
    challenge_id = serializers.UUIDField()
    code = serializers.CharField(min_length=6, max_length=6)

    def validate(self, attrs):
        challenge, error = verify_otp_challenge(
            challenge_id=attrs['challenge_id'],
            code=attrs['code'],
            purpose=AuthOTPChallenge.PURPOSE_SIGNUP_PHONE,
        )

        if error:
            raise serializers.ValidationError(error)

        attrs['challenge'] = challenge
        return attrs

    def save(self):
        from django.utils import timezone
        from decimal import Decimal
        from datetime import date

        challenge = self.validated_data['challenge']
        data = challenge.metadata

        try:
            with transaction.atomic():
                email = data['email'].lower()
                phone_normalized = data['phone_normalized']

                if Customer.objects.select_for_update().filter(
                    Q(email__iexact=email) | Q(phone_normalized=phone_normalized)
                ).exists() or User.objects.select_for_update().filter(email__iexact=email).exists():
                    challenge.status = AuthOTPChallenge.STATUS_USED
                    challenge.save(update_fields=['status'])
                    raise serializers.ValidationError('Unable to complete verification.')

                full_name = f"{data['first_name']} {data['last_name']}".strip()

                portal_user = User.objects.create_user(
                    email=email,
                    password=data['password'],
                    full_name=full_name,
                    phone=data['phone'],
                    phone_normalized=phone_normalized,
                    permission_level=1,
                    user_type='customer',
                    is_staff=False,
                    is_superuser=False,
                    is_active=True,
                )

                customer = Customer.objects.create(
                    portal_user=portal_user,
                    first_name=data['first_name'],
                    last_name=data['last_name'],
                    email=email,
                    phone=data['phone'],
                    phone_normalized=phone_normalized,
                    province=data['province'],
                    date_of_birth=date.fromisoformat(data['date_of_birth']),
                    requested_loan_amount=Decimal(data['requested_loan_amount']),
                    onboarding_stage='banking_verification',
                    status='pending',
                    phone_verified=True,
                    phone_verified_at=timezone.now(),
                    references_completed=False,
                )

                from loans.services import LoanService
                LoanService.create_initial_application(customer)

                challenge.status = AuthOTPChallenge.STATUS_USED
                challenge.save(update_fields=['status'])

                return customer

        except IntegrityError:
            raise serializers.ValidationError('Unable to complete verification.')


class CustomerPortalRequestOTPSerializer(serializers.Serializer):
    identifier = serializers.CharField(max_length=255)

    def validate(self, attrs):
        raw_identifier = attrs["identifier"].strip()

        if "@" in raw_identifier:
            email = User.objects.normalize_email(raw_identifier).lower()
            user = User.objects.filter(
                email__iexact=email,
                user_type="customer",
                is_active=True,
            ).first()

            if not user:
                raise serializers.ValidationError("Unable to process request.")

            attrs["identifier"] = email
            attrs["purpose"] = AuthOTPChallenge.PURPOSE_LOGIN_EMAIL
            attrs["delivery"] = "email"
            attrs["user"] = user
            return attrs

        phone_normalized = normalize_ca_phone(raw_identifier)
        customer = Customer.objects.select_related("portal_user").filter(
            phone_normalized=phone_normalized,
            portal_user__user_type="customer",
            portal_user__is_active=True,
        ).first()

        if not customer or not customer.portal_user:
            raise serializers.ValidationError("Unable to process request.")

        attrs["identifier"] = phone_normalized
        attrs["purpose"] = AuthOTPChallenge.PURPOSE_LOGIN_SMS
        attrs["delivery"] = "sms"
        attrs["user"] = customer.portal_user
        return attrs

    def save(self):
        challenge, code, should_send = create_otp_challenge(
            identifier=self.validated_data["identifier"],
            purpose=self.validated_data["purpose"],
            metadata={"user_id": str(self.validated_data["user"].id)},
        )

        if should_send:
            if self.validated_data["delivery"] == "email":
                send_email_otp_task.delay(self.validated_data["identifier"], code)
            else:
                send_sms_otp_task.delay(self.validated_data["identifier"], code)

        return {
            "challenge_id": challenge.id,
            "delivery": self.validated_data["delivery"],
        }


class CustomerPortalVerifyOTPSerializer(serializers.Serializer):
    challenge_id = serializers.UUIDField()
    code = serializers.CharField(min_length=6, max_length=6)

    def validate(self, attrs):
        challenge = AuthOTPChallenge.objects.filter(
            id=attrs["challenge_id"],
        ).first()

        if not challenge:
            raise serializers.ValidationError("Invalid OTP challenge.")

        if challenge.purpose not in [
            AuthOTPChallenge.PURPOSE_LOGIN_EMAIL,
            AuthOTPChallenge.PURPOSE_LOGIN_SMS,
        ]:
            raise serializers.ValidationError("Invalid OTP challenge.")

        challenge, error = verify_otp_challenge(
            challenge_id=attrs["challenge_id"],
            code=attrs["code"],
            purpose=challenge.purpose,
        )

        if error:
            raise serializers.ValidationError(error)

        user_id = challenge.metadata.get("user_id")
        user = User.objects.filter(
            id=user_id,
            user_type="customer",
            is_active=True,
        ).first()

        if not user:
            raise serializers.ValidationError("Unable to process request.")

        try:
            customer = user.customer_profile
        except Customer.DoesNotExist:
            raise serializers.ValidationError("Unable to process request.")

        attrs["user"] = user
        attrs["customer"] = customer
        attrs["challenge"] = challenge
        return attrs

    def save(self):
        challenge = self.validated_data["challenge"]
        challenge.status = AuthOTPChallenge.STATUS_USED
        challenge.save(update_fields=["status"])

        return self.validated_data["user"], self.validated_data["customer"]


class CustomerPasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate(self, attrs):
        email = User.objects.normalize_email(attrs['email']).lower()

        user = User.objects.filter(
            email__iexact=email,
            user_type='customer',
            is_active=True,
        ).first()

        attrs['user'] = user
        attrs['email'] = email
        return attrs

    def save(self):
        user = self.validated_data.get('user')

        if user:
            challenge, code, should_send = create_otp_challenge(
                identifier=self.validated_data['email'],
                purpose=AuthOTPChallenge.PURPOSE_PASSWORD_RESET_EMAIL,
                metadata={'user_id': str(user.id)},
            )

            if should_send:
                send_email_otp_task.delay(
                    self.validated_data['email'],
                    code,
                )

            return {
                'challenge_id': challenge.id,
                'message': 'Verification code sent.',
            }

        return {
            'challenge_id': None,
            'message': 'If the account exists, a verification code has been sent.',
        }


class CustomerPasswordResetVerifySerializer(serializers.Serializer):
    challenge_id = serializers.UUIDField()
    code = serializers.CharField(min_length=6, max_length=6)

    def validate(self, attrs):
        challenge, error = verify_otp_challenge(
            challenge_id=attrs['challenge_id'],
            code=attrs['code'],
            purpose=AuthOTPChallenge.PURPOSE_PASSWORD_RESET_EMAIL,
        )

        if error:
            raise serializers.ValidationError(error)

        challenge.status = AuthOTPChallenge.STATUS_VERIFIED
        challenge.save(update_fields=['status'])

        attrs['challenge'] = challenge
        return attrs


class CustomerPasswordResetConfirmSerializer(serializers.Serializer):
    challenge_id = serializers.UUIDField()
    password = serializers.CharField(min_length=8)
    confirm_password = serializers.CharField(min_length=8)

    def validate(self, attrs):
        if attrs['password'] != attrs['confirm_password']:
            raise serializers.ValidationError('Passwords do not match.')

        challenge = AuthOTPChallenge.objects.filter(
            id=attrs['challenge_id'],
            purpose=AuthOTPChallenge.PURPOSE_PASSWORD_RESET_EMAIL,
            status=AuthOTPChallenge.STATUS_VERIFIED,
        ).first()

        if not challenge:
            raise serializers.ValidationError('Invalid password reset session.')

        attrs['challenge'] = challenge
        return attrs

    def save(self):
        challenge = self.validated_data['challenge']

        user = User.objects.get(id=challenge.metadata['user_id'])

        user.set_password(self.validated_data['password'])
        user.save(update_fields=['password'])

        challenge.status = AuthOTPChallenge.STATUS_USED
        challenge.save(update_fields=['status'])

        return user

class CustomerPasswordSetupSerializer(serializers.Serializer):
    token = serializers.UUIDField()
    password = serializers.CharField(write_only=True, min_length=8)
    confirm_password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        if attrs['password'] != attrs['confirm_password']:
            raise serializers.ValidationError('Passwords do not match')

        try:
            customer = Customer.objects.select_related('portal_user').get(password_setup_token=attrs['token'])
        except Customer.DoesNotExist:
            raise serializers.ValidationError('Invalid or expired setup token')

        if not customer.portal_user:
            raise serializers.ValidationError('No portal user linked to this customer')

        attrs['customer'] = customer
        return attrs

    def save(self):
        customer = self.validated_data['customer']
        password = self.validated_data['password']

        customer.portal_user.set_password(password)
        customer.portal_user.save(update_fields=['password'])

        customer.onboarding_stage = 'banking_verification'
        customer.password_setup_token = None
        customer.save(update_fields=['onboarding_stage', 'password_setup_token', 'updated_at'])

        return customer


class CustomerPortalLoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        email = attrs.get('email')
        password = attrs.get('password')

        user = authenticate(email=email, password=password)

        if not user:
            raise serializers.ValidationError('Invalid email or password')

        if user.user_type != 'customer':
            raise serializers.ValidationError('Use the staff login')

        if not user.is_active:
            raise serializers.ValidationError('Account is disabled')

        try:
            customer = user.customer_profile
        except Customer.DoesNotExist:
            raise serializers.ValidationError('Customer profile not found')

        attrs['user'] = user
        attrs['customer'] = customer
        return attrs


class CustomerPortalMeSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    onboarding_stage_display = serializers.CharField(source='get_onboarding_stage_display', read_only=True)

    class Meta:
        model = Customer
        fields = [
            'id',
            'first_name',
            'last_name',
            'full_name',
            'email',
            'phone',
            'onboarding_stage',
            'onboarding_stage_display',
            'banking_verified',
            'references_completed',
            'contract_completed',
            'status',
            'created_at',
            'updated_at',
        ]


class CustomerPortalDashboardSerializer(serializers.Serializer):
    customer = CustomerPortalMeSerializer()
    current_application = serializers.DictField(allow_null=True)

    portal_state = serializers.CharField()
    next_step = serializers.CharField()
    next_url = serializers.CharField()

    can_appeal = serializers.BooleanField()
    can_renew = serializers.BooleanField()
    can_refinance = serializers.BooleanField()

    banking = serializers.DictField()


class CustomerJobReferencesSerializer(serializers.Serializer):
    job_place_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    supervisor_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    supervisor_phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    reference_1_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    reference_1_phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    reference_2_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    reference_2_phone = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def save(self, customer):
        field_names = [
            'job_place_name',
            'supervisor_name',
            'supervisor_phone',
            'reference_1_name',
            'reference_1_phone',
            'reference_2_name',
            'reference_2_phone',
        ]

        for field_name in field_names:
            if field_name in self.validated_data:
                value = self.validated_data[field_name]
                setattr(customer, field_name, value.strip() if isinstance(value, str) and value else None)

        customer.references_completed = True
        customer.save()
        return customer


class CustomerPortalLoanSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    type = serializers.CharField()
    type_display = serializers.CharField()
    status = serializers.CharField()
    status_display = serializers.CharField()
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    balance = serializers.DecimalField(max_digits=10, decimal_places=2)
    collected_amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    funded_at = serializers.DateTimeField(allow_null=True)


class GlobalSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = GlobalSetting
        fields = ['key', 'value', 'description', 'is_secret', 'updated_at']


class ApiIntegrationsSerializer(serializers.Serializer):
    flinks_iframe_url = serializers.CharField(required=False, allow_blank=True)
    flinks_instance = serializers.CharField(required=False, allow_blank=True)
    flinks_customer_id = serializers.CharField(required=False, allow_blank=True)
    flinks_secret_key = serializers.CharField(required=False, allow_blank=True)
    zum_api_base_url = serializers.CharField(required=False, allow_blank=True)
    zum_api_key = serializers.CharField(required=False, allow_blank=True)
    zum_webhook_secret = serializers.CharField(required=False, allow_blank=True)
    webhook_url = serializers.CharField(required=False, allow_blank=True)

    def save(self):
        from .models import GlobalSetting
        data = self.validated_data
        mapping = {
            'flinks_iframe_url': 'FLINKS_IFRAME_URL',
            'flinks_instance': 'FLINKS_INSTANCE',
            'flinks_customer_id': 'FLINKS_CUSTOMER_ID',
            'flinks_secret_key': 'FLINKS_SECRET_KEY_CA',
            'zum_api_base_url': 'ZUMRAILS_API_BASE_URL',
            'zum_api_key': 'ZUMRAILS_API_KEY',
            'zum_webhook_secret': 'ZUMRAILS_WEBHOOK_SECRET',
            'webhook_url': 'WEBHOOK_URL',
        }

        for field, key in mapping.items():
            if field in data:
                GlobalSetting.objects.update_or_create(
                    key=key,
                    defaults={'value': data[field]}
                )