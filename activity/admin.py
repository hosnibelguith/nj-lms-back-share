from django.contrib import admin

from .models import ActivityHistory, Comment


@admin.register(ActivityHistory)
class ActivityHistoryAdmin(admin.ModelAdmin):
    list_display = ("id", "customer", "loan", "type", "title", "created_at")
    list_filter = ("type", "created_at")
    search_fields = ("customer__first_name", "customer__last_name", "title", "description")
    ordering = ("-created_at",)
    readonly_fields = ("id", "created_at")
    raw_id_fields = ("customer", "loan")
    date_hierarchy = "created_at"


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ("id", "customer", "loan", "created_by", "is_pinned", "created_at")
    list_filter = ("is_internal", "is_pinned", "created_at")
    search_fields = ("content", "customer__first_name", "customer__last_name")
    ordering = ("-created_at",)
    readonly_fields = ("id", "created_at", "updated_at")
    raw_id_fields = ("customer", "loan", "created_by")
