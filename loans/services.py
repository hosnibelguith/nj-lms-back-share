# loans/services.py
"""
Business logic services for loan operations.
Called by views and celery tasks.
"""
from decimal import Decimal, ROUND_HALF_UP
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from accounts.models import Customer
from .models import Loan, Payment, LoanFormula, FundedPayment


class LoanService:
    """Service class for loan operations."""

    @staticmethod
    def money(value):
        return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @staticmethod
    def get_demo_first_payment_date():
        """
        First demo payment is Thursday of the week after this week.
        """
        today = timezone.localdate()
        days_until_thursday = (3 - today.weekday()) % 7

        this_week_thursday = today + timedelta(days=days_until_thursday)

        if this_week_thursday <= today:
            this_week_thursday += timedelta(days=7)

        return this_week_thursday + timedelta(days=7)

    @staticmethod
    def get_formula_for_amount(amount: Decimal) -> LoanFormula | None:
        """
        Find exact active formula for requested amount.
        Fallback to default active formula if no exact amount match exists.
        """
        formula = LoanFormula.objects.filter(
            principal_amount=amount,
            is_active=True,
        ).order_by('-is_default', '-created_at').first()

        if formula:
            return formula

        return LoanFormula.objects.filter(
            is_active=True,
            is_default=True,
        ).order_by('-created_at').first()

    @staticmethod
    def calculate_from_formula(formula: LoanFormula, principal: Decimal) -> dict:
        brokerage_fee = LoanService.money(
            principal * formula.brokerage_percent / Decimal('100')
        )
        subtotal = LoanService.money(principal + brokerage_fee)

        return {
            'principal': principal,
            'brokerage_fee': brokerage_fee,
            'repayment_fee': Decimal('0.00'),
            'fee': brokerage_fee,
            'total_amount': subtotal,
        }

    @staticmethod
    def calculate_schedule_total(
        principal_balance: Decimal,
        annual_rate_percent: Decimal,
        start_date,
        num_payments: int,
        frequency_days: int,
    ) -> Decimal:
        """
        Interest is annualized and calculated by days outstanding.
        Simple MVP estimate:
        interest for each period = remaining_balance * annual_daily_rate * days
        """
        daily_rate = annual_rate_percent / Decimal('100') / Decimal('365')

        remaining = principal_balance
        total_interest = Decimal('0.00')

        for _ in range(num_payments):
            interest = remaining * daily_rate * Decimal(frequency_days)
            total_interest += interest

            principal_portion = principal_balance / Decimal(num_payments)
            remaining = max(Decimal('0.00'), remaining - principal_portion)

        return LoanService.money(principal_balance + total_interest)

    @staticmethod
    @transaction.atomic
    def create_initial_application(customer: Customer) -> Loan:
        """
        Create the customer's first pending loan application after signup.
        Prevents duplicate active/pending applications.
        """
        blocking_statuses = [
            'pending',
            'pending_signature',
            'ai_approved',
            'review_required',
            'human_approved',
            'pending_funding',
            'active',
        ]

        existing_loan = customer.loans.filter(
            status__in=blocking_statuses
        ).order_by('-created_at').first()

        if existing_loan:
            return existing_loan

        principal = customer.requested_loan_amount or Decimal('0.00')
        formula = LoanService.get_formula_for_amount(principal)

        if formula:
            amounts = LoanService.calculate_from_formula(formula, principal)

            first_date = LoanService.get_demo_first_payment_date()
            total_amount = LoanService.calculate_schedule_total(
                principal_balance=amounts['total_amount'],
                annual_rate_percent=formula.annual_interest_rate,
                start_date=first_date,
                num_payments=formula.default_number_of_payments,
                frequency_days=formula.default_frequency_days,
            )

            fee = total_amount - principal
        else:
            first_date = LoanService.get_demo_first_payment_date()
            fee = Decimal('0.00')
            total_amount = principal + fee

        loan = Loan.objects.create(
            customer=customer,
            formula=formula,
            type='nojuice',
            principal=principal,
            fee=fee,
            total_amount=total_amount,
            balance=total_amount,
            status='pending',
            is_active=True,
            notes='Initial application loan created automatically after customer signup.',
        )

        if formula:
            LoanService.generate_payment_schedule(
                loan=loan,
                num_payments=formula.default_number_of_payments,
                payment_amount=LoanService.money(
                    total_amount / Decimal(formula.default_number_of_payments)
                ),
                start_date=first_date,
                frequency_days=formula.default_frequency_days,
            )

        return loan
    
    @staticmethod
    @transaction.atomic
    def approve_loan(loan: Loan, approved_by=None, notes: str = None, source='human') -> Loan:
        if loan.status not in ['pending', 'review_required', 'ai_approved', 'ai_declined']:
            raise ValueError(f"Cannot approve loan in status: {loan.status}")

        loan.approve(user=approved_by, source=source)

        if loan.contract_signed_at:
            loan.status = 'pending_funding'
            loan.save(update_fields=['status', 'updated_at'])

        if notes:
            loan.notes = ((loan.notes or '') + f"\n{notes}").strip()
            loan.save(update_fields=['notes', 'updated_at'])

        return loan
    
    @staticmethod
    @transaction.atomic
    def decline_loan(loan: Loan, reason: str, declined_by=None, source='human') -> Loan:
        if loan.status not in ['pending', 'pending_signature', 'review_required', 'ai_approved', 'ai_declined', 'human_approved']:
            raise ValueError(f"Cannot decline loan in status: {loan.status}")

        loan.decline(reason=reason, user=declined_by, source=source)
        return loan
    
    @staticmethod
    @transaction.atomic
    def fund_loan(loan: Loan, method: str = 'eft', reference: str = '', user=None) -> Loan:
        if loan.status != 'pending_funding':
            raise ValueError(f"Cannot fund loan in status: {loan.status}")

        ref = reference or f"{method.upper()}-{timezone.now().strftime('%Y%m%d')}-{str(loan.id)[:8].upper()}"

        loan.fund(method, ref, user=user)

        FundedPayment.objects.create(
            loan=loan,
            amount=loan.principal,
            method=method,
            status='completed',
            reference=ref,
            completed_at=timezone.now(),
            notes='Funding created from staff portal.',
        )

        return loan

    @staticmethod
    @transaction.atomic
    def mark_pending_signature(loan: Loan) -> Loan:
        if loan.status not in ['pending', 'ai_approved', 'human_approved', 'review_required']:
            raise ValueError(f"Cannot request signature in status: {loan.status}")

        contract_id = loan.contract_id or f"demo-contract-{str(loan.id)[:8]}"
        loan.mark_contract_sent(contract_id=contract_id)
        return loan

    @staticmethod
    @transaction.atomic
    def sign_customer_contract(customer: Customer) -> Loan:
        loan = customer.loans.filter(
            status__in=['pending_signature', 'pending', 'ai_approved', 'human_approved']
        ).order_by('-created_at').first()

        if not loan:
            raise ValueError('No application available for signature.')

        if not customer.banking_verified:
            raise ValueError('Banking verification must be completed before signing.')

        if loan.status != 'pending_signature':
            LoanService.mark_pending_signature(loan)

        loan.mark_contract_signed()

        customer.contract_completed = True
        customer.onboarding_stage = 'contract'
        customer.save(update_fields=['contract_completed', 'onboarding_stage', 'updated_at'])

        return loan

    @staticmethod
    def mock_ai_decision_for_loan(loan: Loan) -> str:
        outcomes = ['ai_approved', 'ai_declined', 'review_required']
        index = sum(ord(char) for char in str(loan.id)) % len(outcomes)
        return outcomes[index]

    @staticmethod
    @transaction.atomic
    def run_mock_ai_analysis(customer: Customer) -> Loan:
        loan = customer.loans.filter(
            status__in=['pending', 'pending_signature']
        ).order_by('-created_at').first()

        if not loan:
            raise ValueError('No application available for AI analysis.')

        if not customer.banking_verified:
            raise ValueError('Banking verification is required before analysis.')

        decision = LoanService.mock_ai_decision_for_loan(loan)
        previous_status = loan.status

        if decision == 'ai_approved':
            loan.approve(user=None, source='ai')
            if loan.contract_signed_at:
                loan.status = 'pending_funding'
                loan.save(update_fields=['status', 'updated_at'])

        elif decision == 'ai_declined':
            loan.decline(
                reason='AI declined the application based on current verification results.',
                user=None,
                source='ai',
            )

        else:
            loan.status = 'review_required'
            loan.is_active = True
            loan.notes = ((loan.notes or '') + '\nAI decision: review required.').strip()
            loan.save()

            loan.log_state_event(
                event_type='review_required',
                previous_status=previous_status,
                new_status='review_required',
                notes='AI requested human review.',
            )

        return loan
    
    @staticmethod
    @transaction.atomic
    def record_payment(loan: Loan, amount: Decimal, payment_type: str = 'manual') -> Payment:
        """Record a payment on a loan."""
        payment = Payment.objects.create(
            loan=loan,
            amount=amount,
            type=payment_type,
            status='completed',
            scheduled_date=timezone.now().date(),
            processed_at=timezone.now()
        )
        
        # Apply payment to loan balance
        loan.apply_payment(amount)
        return payment
    
    @staticmethod
    @transaction.atomic
    def generate_payment_schedule(loan: Loan, num_payments: int, 
                                  payment_amount: Decimal, 
                                  start_date,
                                  frequency_days: int = 14) -> list:
        """Generate a payment schedule for a loan."""
        payments = []
        current_date = start_date
        remaining = loan.total_amount
        
        for i in range(num_payments):
            is_last = (i == num_payments - 1)
            amt = remaining if is_last else min(payment_amount, remaining)
            remaining -= amt
            
            payment = Payment.objects.create(
                loan=loan,
                amount=amt,
                scheduled_date=current_date,
                type='scheduled',
                status='scheduled'
            )
            payments.append(payment)
            current_date += timedelta(days=frequency_days)
        
        return payments
