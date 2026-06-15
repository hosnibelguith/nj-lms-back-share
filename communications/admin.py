from django.contrib import admin

from .models import Communication, CommunicationTemplate


@admin.register(Communication)
class CommunicationAdmin(admin.ModelAdmin):
    list_display = ("id", "customer", "type", "direction", "subject", "status", "created_at")
    list_filter = ("type", "direction", "status", "created_at")
    search_fields = ("customer__first_name", "customer__last_name", "subject", "to_address", "to_phone")
    ordering = ("-created_at",)
    readonly_fields = ("id", "created_at", "sent_at", "delivered_at", "read_at")
    raw_id_fields = ("customer", "loan", "created_by")
    date_hierarchy = "created_at"


@admin.register(CommunicationTemplate)
class CommunicationTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "type", "trigger", "is_active", "created_at")
    list_filter = ("type", "trigger", "is_active", "created_at")
    search_fields = ("name", "subject")
    ordering = ("name",)
    readonly_fields = ("id", "created_at", "updated_at")
