from .models import BankConnection, BankAccount, BankTransaction
from django.contrib import admin    


admin.site.register(BankConnection)
admin.site.register(BankAccount)
admin.site.register(BankTransaction)

