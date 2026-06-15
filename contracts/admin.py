from django.contrib import admin
from .models import Contract, ContractTemplate


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'customer',
        'loan',
        'status',
        'signed_date',
        'created_at',
    )

    list_filter = (
        'status',
        'created_at',
    )

    search_fields = (
        'customer__email',
        'customer__first_name',
        'customer__last_name',
    )

    readonly_fields = (
        'id',
        'created_at',
        'updated_at',
        'signed_date',
    )


@admin.register(ContractTemplate)
class ContractTemplateAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'loan_type',
        'province',
        'version',
        'is_active',
    )

    list_filter = (
        'loan_type',
        'province',
        'is_active',
    )