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
    def get_interest_breakdown(loan: Loan, as_of_date=None) -> dict:
        """
        Decompose a loan's already-priced fee into brokerage + day-by-day
        interest so staff can explain early-payoff savings or late-payoff
        additions. This reads the values already stored on the loan/formula
        and does NOT re-price the loan.

        planned interest = fee - brokerage fee
        planned duration = number_of_payments * frequency_days
        daily interest   = planned interest / planned duration
        actual interest  = daily interest * days the loan actually existed
        """
        money = LoanService.money
        principal = loan.principal or Decimal('0.00')
        formula = loan.formula

        if formula and principal:
            brokerage_fee = money(
                principal * formula.brokerage_percent / Decimal('100')
            )
        else:
            brokerage_fee = Decimal('0.00')

        planned_interest = (loan.fee or Decimal('0.00')) - brokerage_fee
        if planned_interest < 0:
            planned_interest = Decimal('0.00')
        planned_interest = money(planned_interest)

        if formula:
            planned_days = (
                int(formula.default_number_of_payments)
                * int(formula.default_frequency_days)
            )
        else:
            # No formula attached: derive the planned duration from the
            # loan's own payment schedule span so the timeline still works.
            schedule_dates = list(
                loan.payments.order_by('scheduled_date').values_list(
                    'scheduled_date', flat=True
                )
            )
            if len(schedule_dates) >= 2:
                planned_days = max((schedule_dates[-1] - schedule_dates[0]).days, 0)
            else:
                planned_days = 0

        daily_interest = (
            planned_interest / Decimal(planned_days)
            if planned_days > 0 else Decimal('0.00')
        )

        start_date = loan.funded_at.date() if loan.funded_at else None
        today = as_of_date or timezone.localdate()

        if start_date is None:
            # Not funded yet: only the planned timeline is meaningful.
            charged_days = planned_days
            as_of = today
            projection_start = today
        else:
            if loan.status == 'paid_off':
                last_payment = loan.payments.filter(
                    status='completed'
                ).order_by('-processed_at', '-scheduled_date').first()
                end_date = (
                    last_payment.processed_at.date()
                    if last_payment and last_payment.processed_at
                    else loan.updated_at.date()
                )
            else:
                end_date = today
            charged_days = max((end_date - start_date).days, 0)
            as_of = end_date
            projection_start = start_date

        actual_interest = money(daily_interest * Decimal(charged_days))
        interest_adjustment = money(actual_interest - planned_interest)

        rounded_daily = money(daily_interest)
        base_balance = money(principal + brokerage_fee)
        timeline = []
        for day in range(1, charged_days + 1):
            timeline.append({
                'day': day,
                'type': 'interest' if day <= planned_days else 'late_interest',
                'amount': str(rounded_daily),
                'balance': str(money(base_balance + rounded_daily * Decimal(day))),
                'date': (projection_start + timedelta(days=day)).isoformat(),
            })

        return {
            'capital': str(money(principal)),
            'brokerage_fee': str(brokerage_fee),
            'planned_interest': str(planned_interest),
            'planned_days': planned_days,
            'daily_interest': str(rounded_daily),
            'total_amount': str(money(loan.total_amount or Decimal('0.00'))),
            'funded_at': loan.funded_at.isoformat() if loan.funded_at else None,
            'as_of_date': as_of.isoformat() if as_of else None,
            'elapsed_days': charged_days,
            'is_funded': start_date is not None,
            'status': loan.status,
            'actual_interest': str(actual_interest),
            'interest_adjustment': str(interest_adjustment),
            'timeline': timeline,
        }

    @staticmethod
    @transaction.atomic
    def create_initial_application(customer: Customer) -> Loan:
        """
        Create the customer's first pending loan application after signup.
        Prevents duplicate active/pending applications.
        """
        blocking_statuses = [
            'ibv_pending',
            'pending',
            'pending_signature',
            'pending_funding',
            'active',
        ]

        existing_loan = customer.loans.filter(
            status__in=blocking_statuses
        ).order_by('-created_at').first()

        if existing_loan:
            return existing_loan

        principal = customer.requested_loan_amount or Decimal('0.00')
        if not isinstance(principal, Decimal):
            principal = Decimal(str(principal))
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
            status='ibv_pending',
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
        if source != 'human':
            raise ValueError('AI decisions must be recorded with set_ai_decision.')
        if loan.status not in ['pending', 'pending_signature']:
            raise ValueError(f"Cannot approve loan in status: {loan.status}")

        loan.approve(user=approved_by, source=source)

        if notes:
            loan.notes = ((loan.notes or '') + f"\n{notes}").strip()
            loan.save(update_fields=['notes', 'updated_at'])

        # Arrive card funding is confirmed by staff via Fund Customer → Card Issuance
        # (not auto-funded here). EFT/EMT remains for non-Arrive loans.

        from accounts.arrive_integration import queue_decision_webhook
        queue_decision_webhook(loan, 'approved')

        return loan
    
    @staticmethod
    @transaction.atomic
    def decline_loan(loan: Loan, reason: str, declined_by=None, source='human') -> Loan:
        if source != 'human':
            raise ValueError('AI decisions must be recorded with set_ai_decision.')
        if loan.status not in ['ibv_pending', 'pending', 'pending_signature', 'pending_funding']:
            raise ValueError(f"Cannot decline loan in status: {loan.status}")

        loan.decline(reason=reason, user=declined_by, source=source)

        from accounts.arrive_integration import queue_decision_webhook
        queue_decision_webhook(loan, 'declined')

        template_name = 'Deny Template'
        from communications.models import CommunicationTemplate
        from communications.tasks import send_template_message

        template = CommunicationTemplate.objects.filter(
            name=template_name,
            type='email',
            is_active=True,
        ).first()
        if template and not loan.communications.filter(
            direction='outbound',
            type='email',
            template_name=template_name,
        ).exists():
            customer_id = str(loan.customer_id)
            loan_id = str(loan.id)
            template_id = str(template.id)
            transaction.on_commit(
                lambda: send_template_message.delay(customer_id, template_id, loan_id)
            )

        return loan

    @staticmethod
    @transaction.atomic
    def update_approved_amount(loan: Loan, principal: Decimal, user=None, notes: str = '') -> Loan:
        loan = Loan.objects.select_for_update().get(pk=loan.pk)

        if loan.status not in ['ibv_pending', 'pending_signature', 'pending', 'pending_funding']:
            raise ValueError(f"Cannot update approved amount in status: {loan.status}")
        if loan.funded_payments.filter(status__in=['processing', 'completed']).exists():
            raise ValueError('Cannot update approved amount after funding has started.')

        old_principal = loan.principal
        principal = LoanService.money(principal)
        if principal <= 0:
            raise ValueError('Approved amount must be greater than zero.')

        loan.principal = principal
        loan.save(update_fields=['principal', 'updated_at'])
        LoanService.rebuild_payment_schedule(loan, reprice=True)
        loan.refresh_from_db()

        detail = f'Approved amount changed from ${old_principal} to ${loan.principal}.'
        if notes:
            loan.notes = ((loan.notes or '') + f"\n{notes}").strip()
            loan.save(update_fields=['notes', 'updated_at'])
        loan.log_state_event(
            event_type='amount_updated',
            previous_status=loan.status,
            new_status=loan.status,
            user=user,
            notes=detail,
        )

        return loan
    
    @staticmethod
    @transaction.atomic
    def fund_loan(loan: Loan, method: str = 'eft', reference: str = '', user=None) -> Loan:
        if loan.status != 'pending_funding':
            raise ValueError(f"Cannot fund loan in status: {loan.status}")
        if not loan.contract_signed:
            raise ValueError('Contract must be signed before funding.')

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

        template_name = 'Fund/Approve Template'
        from communications.models import CommunicationTemplate
        from communications.tasks import send_template_message

        template = CommunicationTemplate.objects.filter(
            name=template_name,
            type='email',
            is_active=True,
        ).first()
        if template and not loan.communications.filter(
            direction='outbound',
            type='email',
            template_name=template_name,
        ).exists():
            customer_id = str(loan.customer_id)
            loan_id = str(loan.id)
            template_id = str(template.id)
            transaction.on_commit(
                lambda: send_template_message.delay(customer_id, template_id, loan_id)
            )

        return loan

    @staticmethod
    @transaction.atomic
    def mark_pending_signature(loan: Loan) -> Loan:
        if loan.status not in ['ibv_pending', 'pending']:
            raise ValueError(f"Cannot request signature in status: {loan.status}")

        contract_id = loan.contract_id or f"demo-contract-{str(loan.id)[:8]}"
        loan.mark_contract_sent(contract_id=contract_id)
        return loan

    @staticmethod
    @transaction.atomic
    def sign_customer_contract(customer: Customer) -> Loan:
        loan = customer.loans.filter(
            status__in=['pending_signature', 'pending', 'ibv_pending']
        ).order_by('-created_at').first()

        if not loan:
            raise ValueError('No application available for signature.')

        if not customer.banking_verified:
            raise ValueError('Banking verification must be completed before signing.')

        if loan.status != 'pending_signature':
            LoanService.mark_pending_signature(loan)

        loan.mark_contract_signed()

        customer.contract_completed = True
        customer.onboarding_stage = 'portal_active'
        customer.save(update_fields=['contract_completed', 'onboarding_stage', 'updated_at'])

        return loan

    @staticmethod
    def mock_ai_decision_for_loan(loan: Loan) -> str:
        outcomes = ['approved', 'declined', 'review_required']
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

        notes = ''
        if decision == 'declined':
            notes = 'AI declined the application based on current verification results.'
        elif decision == 'review_required':
            notes = 'AI requested human review.'

        loan.set_ai_decision(decision, notes=notes)

        if loan.status == 'pending_signature' and loan.contract_signed_at:
            loan.status = 'pending'
            loan.save(update_fields=['status', 'updated_at'])

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
    def rebuild_payment_schedule(loan: Loan, *, reprice: bool = True) -> list:
        """
        Replace unprocessed scheduled payments so the calendar matches
        the loan's current principal.

        When reprice=True (default), apply LoanFormula the same way as
        create_initial_application:
          principal + brokerage%  →  subtotal
          + annual_rate/365 interest over the repayment plan  →  total_amount
        Then split total_amount across the formula payment count.
        """
        Payment.objects.filter(loan=loan, status='scheduled').delete()

        principal = LoanService.money(loan.principal or Decimal('0.00'))
        if principal <= 0:
            return []

        formula = LoanService.get_formula_for_amount(principal) or loan.formula
        first_date = LoanService.get_demo_first_payment_date()

        if reprice and formula:
            amounts = LoanService.calculate_from_formula(formula, principal)
            total_amount = LoanService.calculate_schedule_total(
                principal_balance=amounts['total_amount'],
                annual_rate_percent=formula.annual_interest_rate,
                start_date=first_date,
                num_payments=formula.default_number_of_payments,
                frequency_days=formula.default_frequency_days,
            )
            fee = LoanService.money(total_amount - principal)
            loan.formula = formula
            loan.fee = fee
            loan.total_amount = total_amount
            loan.balance = total_amount
            loan.save(
                update_fields=[
                    'formula',
                    'fee',
                    'total_amount',
                    'balance',
                    'updated_at',
                ]
            )
            num_payments = max(1, int(formula.default_number_of_payments or 1))
            frequency_days = int(formula.default_frequency_days or 14)
        elif formula:
            total_amount = LoanService.money(
                loan.total_amount or loan.principal or Decimal('0.00')
            )
            num_payments = max(1, int(formula.default_number_of_payments or 1))
            frequency_days = int(formula.default_frequency_days or 14)
        else:
            total_amount = LoanService.money(
                principal if reprice else (loan.total_amount or loan.principal or Decimal('0.00'))
            )
            if reprice:
                loan.fee = Decimal('0.00')
                loan.total_amount = total_amount
                loan.balance = total_amount
                loan.save(update_fields=['fee', 'total_amount', 'balance', 'updated_at'])
            num_payments = 1
            frequency_days = 14

        if total_amount <= 0:
            return []

        payment_amount = LoanService.money(total_amount / Decimal(num_payments))
        return LoanService.generate_payment_schedule(
            loan=loan,
            num_payments=num_payments,
            payment_amount=payment_amount,
            start_date=first_date,
            frequency_days=frequency_days,
        )
    
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
