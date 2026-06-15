from django.contrib import admin

from .models import Loan, Payment


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    readonly_fields = ("id", "created_at", "processed_at")
    fields = ("scheduled_date", "amount", "type", "status", "reference", "processed_at")


@admin.register(Loan)
class LoanAdmin(admin.ModelAdmin):
    list_display = ("id", "customer", "type", "principal", "balance", "status", "created_at")
    list_filter = ("status", "type", "created_at")
    search_fields = ("customer__first_name", "customer__last_name", "customer__email")
    ordering = ("-created_at",)
    readonly_fields = ("id", "total_amount", "created_at", "updated_at", "approved_at", "funded_at", "contract_sent_at", "contract_signed_at", "declined_at")
    raw_id_fields = ("customer", "bank_account", "approved_by")
    date_hierarchy = "created_at"
    inlines = [PaymentInline]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "loan", "scheduled_date", "amount", "type", "status", "created_at")
    list_filter = ("status", "type", "scheduled_date")
    search_fields = ("loan__customer__first_name", "loan__customer__last_name")
    ordering = ("-scheduled_date",)
    readonly_fields = ("id", "created_at", "processed_at")
    raw_id_fields = ("loan", "created_by")
    date_hierarchy = "scheduled_date"
