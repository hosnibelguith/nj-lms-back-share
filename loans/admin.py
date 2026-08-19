from django.contrib import admin

from .models import (
    BankHoliday,
    CollectionPayment,
    CollectionsAccountChangeAudit,
    FundedPayment,
    FundingMethodRecommendation,
    Loan,
    Payment,
    WebhookEvent,
)


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    readonly_fields = ("id", "created_at", "processed_at")
    fields = ("scheduled_date", "original_date", "amount", "type", "status", "reference", "processed_at")


class FundedPaymentInline(admin.TabularInline):
    model = FundedPayment
    extra = 0
    readonly_fields = ("id", "processor_transaction_id", "initiated_at", "completed_at", "created_at", "updated_at")
    fields = ("amount", "method", "status", "processor_transaction_id", "initiated_at", "completed_at")


class CollectionPaymentInline(admin.TabularInline):
    model = CollectionPayment
    extra = 0
    readonly_fields = ("id", "processor_transaction_id", "initiated_at", "settlement_due_at", "settled_at")
    fields = ("amount", "status", "zum_status", "processor_transaction_id", "initiated_at", "settlement_due_at", "settled_at")


@admin.register(Loan)
class LoanAdmin(admin.ModelAdmin):
    list_display = ("id", "customer", "type", "principal", "balance", "status", "created_at")
    list_filter = ("status", "type", "created_at")
    search_fields = ("customer__first_name", "customer__last_name", "customer__email")
    ordering = ("-created_at",)
    readonly_fields = ("id", "total_amount", "created_at", "updated_at", "approved_at", "funded_at", "contract_sent_at", "contract_signed_at", "declined_at")
    raw_id_fields = ("customer", "bank_account", "collections_account", "approved_by")
    date_hierarchy = "created_at"
    inlines = [PaymentInline, FundedPaymentInline, CollectionPaymentInline]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "loan", "scheduled_date", "amount", "type", "status", "created_at")
    list_filter = ("status", "type", "scheduled_date")
    search_fields = ("loan__customer__first_name", "loan__customer__last_name")
    ordering = ("-scheduled_date",)
    readonly_fields = ("id", "created_at", "processed_at")
    raw_id_fields = ("loan", "created_by")
    date_hierarchy = "scheduled_date"


@admin.register(FundingMethodRecommendation)
class FundingMethodRecommendationAdmin(admin.ModelAdmin):
    list_display = ("weekday", "method", "is_active", "updated_at")
    list_filter = ("method", "is_active")
    ordering = ("weekday",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(FundedPayment)
class FundedPaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "loan", "amount", "method", "status", "processor_transaction_id", "initiated_at")
    list_filter = ("status", "method", "initiated_at")
    search_fields = ("processor_transaction_id", "loan__customer__email")
    readonly_fields = ("id", "created_at", "updated_at")
    raw_id_fields = ("loan", "initiated_by")
    date_hierarchy = "initiated_at"


@admin.register(CollectionPayment)
class CollectionPaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "loan", "amount", "status", "zum_status", "processor_transaction_id", "initiated_at", "settlement_due_at")
    list_filter = ("status", "zum_status", "initiated_at")
    search_fields = ("processor_transaction_id", "loan__customer__email")
    readonly_fields = ("id", "created_at", "updated_at")
    raw_id_fields = ("loan", "payment", "initiated_by")
    date_hierarchy = "initiated_at"


@admin.register(CollectionsAccountChangeAudit)
class CollectionsAccountChangeAuditAdmin(admin.ModelAdmin):
    list_display = ("id", "loan", "changed_by", "changed_at", "failed_payment")
    search_fields = ("loan__customer__email", "failure_reason")
    readonly_fields = ("id", "changed_at")
    raw_id_fields = ("loan", "changed_by", "failed_payment")
    date_hierarchy = "changed_at"


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("id", "webhook_type", "event_name", "processor_transaction_id", "received_at", "processed_at")
    list_filter = ("webhook_type", "event_name", "received_at")
    search_fields = ("processor_transaction_id", "payload_hash")
    readonly_fields = ("id", "received_at", "processed_at")
    date_hierarchy = "received_at"


@admin.register(BankHoliday)
class BankHolidayAdmin(admin.ModelAdmin):
    list_display = ("date", "name", "created_at")
    list_filter = ("date",)
    search_fields = ("name",)
    ordering = ("date",)
    date_hierarchy = "date"
