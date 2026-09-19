from django.contrib import admin

from .access import mohawk_staff
from .models import DashboardSnapshot, PluginSettings, RejectDelivery


class IntegrationAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_superuser and mohawk_staff(request.user)

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PluginSettings)
class PluginSettingsAdmin(IntegrationAdmin):
    list_display = ('__str__', 'enabled', 'capture_since', 'updated_at')
    fields = ('enabled', 'capture_since', 'updated_at')
    readonly_fields = ('capture_since', 'updated_at')


@admin.register(RejectDelivery)
class RejectDeliveryAdmin(IntegrationAdmin):
    list_display = ('source_id', 'status', 'attempts', 'created_at', 'delivered_at', 'last_error')
    list_filter = ('status',)
    search_fields = ('source_id', 'remote_row_id')
    exclude = ('payload', 'lender_id')
    actions = ['retry_corrected_rows']

    @admin.action(description='Retry selected rows after correcting source data')
    def retry_corrected_rows(self, request, queryset):
        if not self.has_module_permission(request):
            return
        from .services import retry_corrected
        count = sum(retry_corrected(pk) for pk in queryset.filter(status='blocked').values_list('pk', flat=True))
        self.message_user(request, f'{count} rows queued with corrected source data.')

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(DashboardSnapshot)
class DashboardSnapshotAdmin(IntegrationAdmin):
    list_display = ('id', 'fetched_at', 'attempted_at', 'last_error')
    exclude = ('payload',)

    def has_change_permission(self, request, obj=None):
        return False
