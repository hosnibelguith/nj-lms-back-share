from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User, Customer, GlobalSetting

@admin.register(GlobalSetting)
class GlobalSettingAdmin(admin.ModelAdmin):
    list_display = ("key", "value", "is_secret", "updated_at")
    search_fields = ("key", "value", "description")
    list_filter = ("is_secret", "updated_at")
    readonly_fields = ("created_at", "updated_at")

@admin.register(User)
class UserAdmin(BaseUserAdmin):
    # Added 'user_type' to the list so you can see it at a glance
    list_display = ("email", "full_name", "user_type", "permission_level", "is_staff", "is_active")
    list_filter = ("user_type", "permission_level", "is_staff", "is_superuser", "is_active")
    search_fields = ("email", "full_name", "phone")
    ordering = ("email",)
    readonly_fields = ("created_at", "updated_at", "last_login")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("full_name", "phone")}),
        (
            "Permissions",
            {
                "fields": (
                    "user_type",  # Added this so you can manually fix your account
                    "permission_level",
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Important dates", {"fields": ("last_login", "created_at", "updated_at")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "full_name", "password1", "password2", "user_type", "permission_level"),
            },
        ),
    )

@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "phone", "province", "status", "source", "created_at")
    list_filter = ("status", "province", "source", "created_at")
    search_fields = (
        "first_name",
        "last_name",
        "email",
        "phone",
        "arrive_application_id",
        "arrive_zum_user_id",
        "arrive_event_id",
    )
    ordering = ("-created_at",)
    readonly_fields = ("id", "created_at", "updated_at")
    fieldsets = (
        (None, {
            "fields": (
                "id",
                "portal_user",
                "first_name",
                "last_name",
                "email",
                "phone",
                "phone_normalized",
                "status",
                "source",
            ),
        }),
        ("Arrive linkage", {
            "fields": (
                "arrive_application_id",
                "arrive_zum_user_id",
                "arrive_zum_user_card_id",
                "arrive_event_id",
            ),
        }),
        ("Onboarding", {
            "fields": (
                "onboarding_stage",
                "banking_verified",
                "references_completed",
                "contract_completed",
                "phone_verified",
                "requested_loan_amount",
            ),
        }),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )