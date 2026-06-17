import random
from decimal import Decimal
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from faker import Faker

# Models
from accounts.models import Customer
from loans.models import Loan, Payment
from banking.models import BankConnection, BankAccount, BankTransaction, FinancialAnalysisReport

fake = Faker()

class Command(BaseCommand):
    help = 'Seeds the database with 20 customers, bank data, loans, and payments'

    def handle(self, *args, **kwargs):
        self.stdout.write(self.style.SUCCESS('Starting data seeding with updated models...'))

        for _ in range(20):
            # 1. Create Customer
            customer = Customer.objects.create(
                first_name=fake.first_name(),
                last_name=fake.last_name(),
                email=fake.unique.email(),
                phone=fake.phone_number()[:20],
                address_line_1=fake.street_address(),
                city=fake.city(),
                province=random.choice(['ON', 'BC', 'AB', 'QC']),
                postal_code=fake.postcode()[:10],
                status='active'
            )

            # 2. Create Bank Connection (New Requirement)
            connection = BankConnection.objects.create(
                customer=customer,
                login_id=f"login_{fake.uuid4()[:8]}",
                provider='flinks',
                sync_status='synced',
                last_synced_at=timezone.now()
            )

            # 3. Create Bank Account (Linked to Connection)
            bank_account = BankAccount.objects.create(
                connection=connection,
                customer=customer,
                external_id=fake.uuid4(),
                name=f"{random.choice(['Standard', 'Premium', 'Direct'])} Checking",
                type='checking',
                currency='CAD',
                balance=Decimal(random.randint(1000, 5000)),
                institution_number=str(random.randint(100, 999)),
                transit_number=str(random.randint(10000, 99999)),
                account_number=f"{random.randint(1000000, 9999999)}",
                is_primary=True
            )

            # 4. Create some dummy Bank Transactions (For Realism)
            for i in range(10):
                is_debit = random.choice([True, False])
                amt = Decimal(random.randint(10, 500))
                BankTransaction.objects.create(
                    account=bank_account,
                    customer=customer,
                    external_id=fake.uuid4(),
                    date=timezone.now().date() - timedelta(days=i*3),
                    description=fake.company(),
                    debit=amt if is_debit else None,
                    credit=None if is_debit else amt,
                    balance=bank_account.balance - (amt if is_debit else -amt)
                )

            # 5. Create Financial Report
            FinancialAnalysisReport.objects.create(
                customer=customer,
                report_data={
                    "avg_monthly_income": 3500.00,
                    "risk_score": random.randint(1, 100),
                    "detected_employer": fake.company()
                }
            )

            # 6. Create 1-2 Loans per customer
            for _ in range(random.randint(1, 2)):
                principal = Decimal(random.randint(300, 1000))
                fee = Decimal('15.00') * (principal / 100) # Simple fee math
                status = random.choice(['active', 'paid_off', 'pending_funding', 'pending', 'human_approved'])
                
                # Logic for balance
                total = principal + fee
                balance = 0 if status == 'paid_off' else total - Decimal(random.randint(0, int(total/2)))

                loan = Loan.objects.create(
                    customer=customer,
                    bank_account=bank_account,
                    type='nojuice',
                    principal=principal,
                    fee=fee,
                    total_amount=total,
                    balance=balance,
                    status=status
                )

                # 7. Create Payments for the Loan
                for i in range(3):
                    p_date = timezone.now().date() + timedelta(days=(i * 14) - 14)
                    p_status = 'completed' if p_date < timezone.now().date() else 'scheduled'
                    
                    if status == 'paid_off':
                        p_status = 'completed'

                    Payment.objects.create(
                        loan=loan,
                        amount=(total / 3).quantize(Decimal('0.01')),
                        type='scheduled',
                        status=p_status,
                        scheduled_date=p_date,
                        processed_at=timezone.now() if p_status == 'completed' else None
                    )

        self.stdout.write(self.style.SUCCESS('Successfully seeded 20 customers with full banking and loan history!'))