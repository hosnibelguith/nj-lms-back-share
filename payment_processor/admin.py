from django.contrib import admin
from .access import is_owner, OWNER_EMAIL
from .models import PluginSettings, SyncRun, WebhookInbox, Operation


class OwnerAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return is_owner(request.user)

    def has_view_permission(self, request, obj=None):
        return is_owner(request.user)

    def has_change_permission(self, request, obj=None):
        return is_owner(request.user)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PluginSettings)
class PluginSettingsAdmin(OwnerAdmin):
    list_display = ('__str__', 'enabled', 'allow_provider_writes', 'updated_at')
    readonly_fields = ('owner', 'updated_at')
    fields = ('owner', 'enabled', 'allow_provider_writes', 'reconcile_minutes', 'read_existing_webhooks', 'updated_at')

    @admin.display(description='Only account allowed in this plugin')
    def owner(self, obj):
        return OWNER_EMAIL

    def has_add_permission(self, request):
        return is_owner(request.user) and not PluginSettings.objects.using('default').exists()


class AuditAdmin(OwnerAdmin):
    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SyncRun)
class SyncRunAdmin(AuditAdmin):
    list_display = ('resource', 'status', 'fetched_count', 'expected_count', 'started_at', 'completed_at', 'error')
    list_filter = ('resource', 'status')


@admin.register(WebhookInbox)
class WebhookAdmin(AuditAdmin):
    list_display = ('id', 'source', 'received_at', 'processed_at', 'attempts', 'error')


@admin.register(Operation)
class OperationAdmin(AuditAdmin):
    list_display = ('id', 'actor_email', 'action', 'status', 'created_at', 'executed_at')
