from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User, Customer

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
    list_display = ("full_name", "email", "phone", "province", "status", "created_at")
    list_filter = ("status", "province", "created_at")
    search_fields = ("first_name", "last_name", "email", "phone")
    ordering = ("-created_at",)
    readonly_fields = ("id", "created_at", "updated_at")