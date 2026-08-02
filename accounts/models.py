from django.db import models
from django.contrib.auth.models import AbstractUser, BaseUserManager
import uuid
from django.utils import timezone


class UserManager(BaseUserManager):
    """Custom user manager for staff users and customer portal users."""
    
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Users must have an email address')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('permission_level', 5)
        extra_fields.setdefault('user_type', 'staff')
        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    """
    Unified auth user model.
    Used for:
    - Staff/admin users
    - Customer portal users
    """
    PERMISSION_LEVELS = [
        (1, 'Viewer'),
        (2, 'Agent'),
        (3, 'Senior Agent'),
        (4, 'Manager'),
        (5, 'Admin'),
    ]

    USER_TYPE_CHOICES = [
        ('staff', 'Staff'),
        ('customer', 'Customer'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = None
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255)
    phone = models.CharField(max_length=20, blank=True, null=True)
    phone_normalized = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        db_index=True,
    )
    flinks_email = models.EmailField(blank=True, null=True)
    flinks_phone = models.CharField(max_length=20, blank=True, null=True)
    flinks_name = models.CharField(max_length=255, blank=True, null=True)
    permission_level = models.PositiveSmallIntegerField(choices=PERMISSION_LEVELS, default=1)
    user_type = models.CharField(max_length=20, choices=USER_TYPE_CHOICES, default='staff', db_index=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    objects = UserManager()
    
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['full_name']
    
    class Meta:
        db_table = 'accounts_user'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.full_name} ({self.email})"
    
    def has_permission(self, required_level: int) -> bool:
        return self.permission_level >= required_level


class Customer(models.Model):
    """
    Customer model - loan applicants/borrowers.
    Linked to a portal auth user.
    """
    PROVINCE_CHOICES = [
        ('ON', 'Ontario'),
        ('BC', 'British Columbia'),
        ('AB', 'Alberta'),
        ('QC', 'Quebec'),
        ('MB', 'Manitoba'),
        ('SK', 'Saskatchewan'),
        ('NS', 'Nova Scotia'),
        ('NB', 'New Brunswick'),
        ('NL', 'Newfoundland and Labrador'),
        ('PE', 'Prince Edward Island'),
        ('NT', 'Northwest Territories'),
        ('YT', 'Yukon'),
        ('NU', 'Nunavut'),
    ]
    
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('collections', 'Collections'),
        ('renewals', 'Renewals'),
        ('refinances', 'Refinances'),
    ]

    ONBOARDING_STAGE_CHOICES = [
        ('password_setup', 'Password Setup'),
        ('banking_verification', 'Banking Verification'),
        ('references', 'References'),
        ('contract', 'Contract'),
        ('portal_active', 'Portal Active'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    portal_user = models.OneToOneField(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='customer_profile'
    )

    first_name = models.CharField(max_length=100, db_index=True)
    last_name = models.CharField(max_length=100, db_index=True)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, db_index=True)
    phone_normalized = models.CharField(
        max_length=20,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
    )

    date_of_birth = models.DateField(null=True, blank=True)
    address_line_1 = models.CharField(max_length=255, blank=True, null=True)
    address_line_2 = models.CharField(max_length=255, blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    province = models.CharField(max_length=2, choices=PROVINCE_CHOICES, blank=True, null=True, db_index=True)
    postal_code = models.CharField(max_length=10, blank=True, null=True)
    country = models.CharField(max_length=100, default='Canada')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active', db_index=True)

    onboarding_stage = models.CharField(
        max_length=30,
        choices=ONBOARDING_STAGE_CHOICES,
        default='password_setup',
        db_index=True
    )
    password_setup_token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, null=True, blank=True)
    banking_verified = models.BooleanField(default=False)
    references_completed = models.BooleanField(default=False)
    contract_completed = models.BooleanField(default=False)

    phone_verified = models.BooleanField(default=False, db_index=True)
    phone_verified_at = models.DateTimeField(null=True, blank=True)

    # Set by a STOP reply or an unsubscribe delivery status; blocks all outbound SMS.
    sms_opted_out = models.BooleanField(default=False, db_index=True)
    sms_opted_out_at = models.DateTimeField(null=True, blank=True)
    sms_opt_out_reason = models.CharField(max_length=255, blank=True, null=True)

    requested_loan_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )

    SOURCE_ORGANIC = 'organic'
    SOURCE_ARRIVE = 'arrive'
    SOURCE_CHOICES = [
        (SOURCE_ORGANIC, 'Organic'),
        (SOURCE_ARRIVE, 'Arrive'),
    ]
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default=SOURCE_ORGANIC,
        db_index=True,
    )
    arrive_application_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        unique=True,
        db_index=True,
    )
    arrive_zum_user_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    arrive_zum_user_card_id = models.CharField(max_length=100, blank=True, null=True)
    arrive_event_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        unique=True,
        db_index=True,
    )

    job_place_name = models.CharField(max_length=255, blank=True, null=True)
    supervisor_name = models.CharField(max_length=255, blank=True, null=True)
    supervisor_phone = models.CharField(max_length=20, blank=True, null=True)

    reference_1_name = models.CharField(max_length=255, blank=True, null=True)
    reference_1_phone = models.CharField(max_length=20, blank=True, null=True)

    reference_2_name = models.CharField(max_length=255, blank=True, null=True)
    reference_2_phone = models.CharField(max_length=20, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'accounts_customer'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at'], name='acct_cust_status_created_idx'),
            models.Index(fields=['province', '-created_at'], name='acct_cust_province_created_idx'),
            models.Index(fields=['last_name', 'first_name'], name='acct_cust_name_idx'),
            models.Index(fields=['onboarding_stage', '-created_at'], name='acct_cust_stage_created_idx'),
        ]
    
    def __str__(self):
        return f"{self.first_name} {self.last_name}"
    
    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()
    
    @property
    def full_address(self):
        parts = [self.address_line_1, self.address_line_2, self.city, self.province, self.postal_code]
        return ', '.join(filter(None, parts))


class AuthOTPChallenge(models.Model):
    PURPOSE_SIGNUP_PHONE = 'signup_phone'
    PURPOSE_LOGIN_EMAIL = 'login_email'
    PURPOSE_LOGIN_SMS = 'login_sms'

    PURPOSE_PASSWORD_RESET_EMAIL = 'password_reset_email'
    PURPOSE_PASSWORD_RESET_SMS = 'password_reset_sms'

    PURPOSE_CHOICES = [
        (PURPOSE_SIGNUP_PHONE, 'Signup Phone Verification'),
        (PURPOSE_LOGIN_EMAIL, 'Login Email OTP'),
        (PURPOSE_LOGIN_SMS, 'Login SMS OTP'),
        (PURPOSE_PASSWORD_RESET_EMAIL, 'Password Reset Email OTP'),
        (PURPOSE_PASSWORD_RESET_SMS, 'Password Reset SMS OTP'),
    ]

    STATUS_PENDING = 'pending'
    STATUS_VERIFIED = 'verified'
    STATUS_EXPIRED = 'expired'
    STATUS_LOCKED = 'locked'
    STATUS_USED = 'used'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_VERIFIED, 'Verified'),
        (STATUS_EXPIRED, 'Expired'),
        (STATUS_LOCKED, 'Locked'),
        (STATUS_USED, 'Used'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    purpose = models.CharField(max_length=30, choices=PURPOSE_CHOICES, db_index=True)
    identifier = models.CharField(max_length=255, db_index=True)
    code_hash = models.CharField(max_length=128)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'accounts_auth_otp_challenge'
        indexes = [
            models.Index(fields=['identifier', 'purpose', 'status', '-created_at'], name='acct_otp_lookup_idx'),
        ]

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at


class GlobalSetting(models.Model):
    """
    Store system-wide configuration keys and values.
    Used for API credentials, integration toggles, and global defaults.
    """
    key = models.CharField(max_length=100, unique=True, db_index=True)
    value = models.TextField(blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    is_secret = models.BooleanField(default=False, help_text="If True, UI should mask this value.")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'accounts_global_setting'
        ordering = ['key']

    def __str__(self):
        return f"{self.key}: {self.value}"

    @classmethod
    def get_value(cls, key, default=None):
        try:
            return cls.objects.get(key=key).value
        except cls.DoesNotExist:
            return default


class ArriveHandoffToken(models.Model):
    """One-time SSO token for Arrive iframe handoff into the customer portal."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='arrive_handoff_tokens',
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'accounts_arrive_handoff_token'
        ordering = ['-created_at']

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    @property
    def is_consumed(self):
        return self.consumed_at is not None

    def mark_consumed(self):
        self.consumed_at = timezone.now()
        self.save(update_fields=['consumed_at'])
