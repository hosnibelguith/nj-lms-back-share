# loans/services.py
"""
Business logic services for loan operations.
Called by views and celery tasks.
"""
import re
from decimal import Decimal, ROUND_HALF_UP
from datetime import timedelta
from django.db import models, transaction
from django.utils import timezone
from accounts.models import Customer
from . import business_calendar
from .models import CollectionPayment, FundedPayment, Loan, LoanFormula, Payment


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
        blocking_statuses = list(LoanService.BLOCKING_APPLICATION_STATUSES)

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

    BLOCKING_APPLICATION_STATUSES = (
        'ibv_pending',
        'pending',
        'pending_signature',
        'pending_funding',
        'active',
    )

    @staticmethod
    def can_start_new_application(customer: Customer) -> bool:
        """Terminal declined/expired applicants can open a fresh application."""
        loans = customer.loans.all()
        if loans.filter(status__in=LoanService.BLOCKING_APPLICATION_STATUSES).exists():
            return False
        return loans.filter(status__in=['human_declined', 'expired']).exists()

    @staticmethod
    @transaction.atomic
    def start_new_application(customer: Customer) -> Loan:
        """
        Open a fresh application for a declined or expired customer.

        Previous loans are kept. Customer onboarding is reset so IBV and
        contract run again on the new loan.
        """
        if customer.loans.filter(
            status__in=LoanService.BLOCKING_APPLICATION_STATUSES
        ).exists():
            raise ValueError(
                'A new application cannot be started while another '
                'application or loan is in progress.'
            )
        if not customer.loans.filter(status__in=['human_declined', 'expired']).exists():
            raise ValueError('Only declined or expired applicants can start a new application.')

        from banking.models import BankConnection
        from activity.services import log_staff_action

        BankConnection.objects.filter(customer=customer, is_active=True).update(
            is_active=False
        )

        customer.banking_verified = False
        customer.contract_completed = False
        customer.onboarding_stage = 'banking_verification'
        customer.save(
            update_fields=[
                'banking_verified',
                'contract_completed',
                'onboarding_stage',
                'updated_at',
            ]
        )

        loan = LoanService.create_initial_application(customer)

        log_staff_action(
            customer=customer,
            loan=loan,
            user=getattr(customer, 'portal_user', None),
            type_value='system',
            title='New Application Started',
            description=(
                'Customer started a new application after a previous decline. '
                'Previous loans were kept. Banking and contract must be completed again.'
            ),
            metadata={'action': 'start_new_application'},
        )
        return loan

    @staticmethod
    @transaction.atomic
    def approve_loan(loan: Loan, approved_by=None, notes: str = None, source='human') -> Loan:
        if source != 'human':
            raise ValueError('AI decisions must be recorded with set_ai_decision.')
        if loan.status not in ['pending', 'pending_signature']:
            raise ValueError(f"Cannot approve loan in status: {loan.status}")

        from activity.services import actor_label, log_staff_action

        previous_display = loan.get_status_display()
        previous_status = loan.status
        loan.approve(user=approved_by, source=source)

        if notes:
            loan.notes = ((loan.notes or '') + f"\n{notes}").strip()
            loan.save(update_fields=['notes', 'updated_at'])

        actor = actor_label(approved_by)
        description = (
            f'Approved by {actor}. '
            f'Status changed from {previous_display} to {loan.get_status_display()}.'
        )
        if notes:
            description = f'{description} Notes: {notes.strip()}'
        log_staff_action(
            customer=loan.customer,
            loan=loan,
            user=approved_by,
            type_value='system',
            title='Loan Approved',
            description=description,
            metadata={
                'previous_status': previous_status,
                'new_status': loan.status,
                'action': 'approve',
            },
        )

        # Arrive card funding is confirmed by staff via Fund Customer → Card Issuance
        # (not auto-funded here). EFT/EMT remains for non-Arrive loans.

        from accounts.arrive_integration import queue_decision_webhook
        queue_decision_webhook(loan, 'approved')

        return loan
    
    @staticmethod
    @transaction.atomic
    def decline_loan(
        loan: Loan,
        reason: str,
        declined_by=None,
        source='human',
        reason_label: str | None = None,
        comment: str = '',
    ) -> Loan:
        if source != 'human':
            raise ValueError('AI decisions must be recorded with set_ai_decision.')
        if loan.status not in ['ibv_pending', 'pending', 'pending_signature', 'pending_funding']:
            raise ValueError(f"Cannot decline loan in status: {loan.status}")

        from activity.services import actor_label, log_staff_action

        previous_display = loan.get_status_display()
        previous_status = loan.status
        loan.decline(reason=reason, user=declined_by, source=source)

        label = (reason_label or reason.split('\n', 1)[0]).strip()
        actor = actor_label(declined_by)
        description = (
            f'Declined by {actor}. '
            f'Status changed from {previous_display} to {loan.get_status_display()}. '
            f'Reason: {label}.'
        )
        if comment:
            description = f'{description} Comment: {comment.strip()}'
        log_staff_action(
            customer=loan.customer,
            loan=loan,
            user=declined_by,
            type_value='comment',
            title='Loan Declined',
            description=description,
            metadata={
                'decline_reason': label,
                'comment': (comment or '').strip(),
                'previous_status': previous_status,
                'new_status': loan.status,
                'action': 'decline',
            },
        )

        from accounts.arrive_integration import queue_decision_webhook
        queue_decision_webhook(loan, 'declined')

        decline_template_names = ['Deny Template', 'DENIED']
        from communications.models import CommunicationTemplate
        from communications.tasks import send_template_message

        template = CommunicationTemplate.objects.filter(
            name__in=decline_template_names,
            type='email',
            is_active=True,
        ).order_by('name').first()
        if template and not loan.communications.filter(
            direction='outbound',
            type='email',
            template_name__in=decline_template_names,
        ).exists():
            customer_id = str(loan.customer_id)
            loan_id = str(loan.id)
            template_id = str(template.id)
            transaction.on_commit(
                lambda: send_template_message.delay(customer_id, template_id, loan_id)
            )

        return loan

    @staticmethod
    def _customer_is_arrive(customer: Customer) -> bool:
        return bool(
            getattr(customer, 'source', None) == Customer.SOURCE_ARRIVE
            or getattr(customer, 'arrive_application_id', None)
        )

    @staticmethod
    @transaction.atomic
    def expire_unsigned_contract(loan: Loan, expired_by=None, comment: str = '') -> Loan:
        """Cancel an Arrive application whose contract was never signed.

        Landing unsigned contracts stay on reminders + staff decline. Missing IBV
        still auto-expires after the reminder window.
        """
        loan = Loan.objects.select_for_update().select_related('customer').get(pk=loan.pk)
        if not LoanService._customer_is_arrive(loan.customer):
            raise ValueError(
                'Only Arrive applications can be cancelled as expired for a missing contract.'
            )
        if loan.status not in ('pending_signature', 'pending_funding'):
            raise ValueError('Only unsigned Arrive contracts can be cancelled as expired.')
        if loan.contract_signed:
            raise ValueError('This contract is already signed.')
        if loan.funded_payments.filter(status__in=['processing', 'completed']).exists():
            raise ValueError('Cannot expire a loan after funding has started.')

        from django.conf import settings
        from activity.services import actor_label, log_staff_action
        from accounts.arrive_integration import queue_decision_webhook
        from communications.models import CommunicationTemplate
        from communications.tasks import send_template_message

        previous_status = loan.status
        previous_display = loan.get_status_display()
        loan.status = 'expired'
        loan.is_active = False
        loan.decline_reason = 'expired'
        loan._suppress_status_activity = True
        loan.save(update_fields=['status', 'is_active', 'decline_reason', 'updated_at'])
        loan.log_state_event(
            event_type='expired',
            previous_status=previous_status,
            new_status=loan.status,
            user=expired_by,
            notes='Unsigned contract cancelled as expired.',
        )

        actor = actor_label(expired_by)
        description = (
            f'Unsigned contract cancelled as expired by {actor}. '
            f'Status changed from {previous_display} to {loan.get_status_display()}.'
        )
        if comment:
            description = f'{description} Comment: {comment.strip()}'
        log_staff_action(
            customer=loan.customer,
            loan=loan,
            user=expired_by,
            type_value='system',
            title='Loan Expired',
            description=description,
            metadata={
                'decline_reason': 'expired',
                'comment': (comment or '').strip(),
                'previous_status': previous_status,
                'new_status': loan.status,
                'action': 'expire_unsigned_contract',
            },
        )
        queue_decision_webhook(loan, 'declined')

        template = CommunicationTemplate.objects.filter(
            name='Application Expired Template',
            type='email',
            is_active=True,
        ).first()
        if template and not loan.communications.filter(
            direction='outbound',
            type='email',
            template_name='Application Expired Template',
        ).exists():
            frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')
            customer_id = str(loan.customer_id)
            loan_id = str(loan.id)
            template_id = str(template.id)
            extra_context = {'portal_url': f'{frontend_url}/customer/login'}
            transaction.on_commit(
                lambda: send_template_message.delay(
                    customer_id,
                    template_id,
                    loan_id,
                    extra_context=extra_context,
                )
            )

        return loan

    @staticmethod
    @transaction.atomic
    def revert_decline_to_approve(loan: Loan, approved_by=None, notes: str = None) -> Loan:
        """Undo a human decline and move the loan back to approved (pending funding)."""
        if loan.status != 'human_declined':
            raise ValueError(f"Cannot revert decline for loan in status: {loan.status}")

        from activity.services import actor_label, log_staff_action

        previous_status = loan.status
        previous_display = loan.get_status_display()
        loan.status = 'pending_funding'
        loan.is_active = True
        loan.approved_at = timezone.now()
        loan.approved_by = approved_by
        loan.declined_at = None
        loan.decline_reason = ''
        if notes:
            loan.notes = ((loan.notes or '') + f"\n{notes}").strip()
        loan._suppress_status_activity = True
        loan.save()

        loan.log_state_event(
            event_type='human_approved',
            previous_status=previous_status,
            new_status=loan.status,
            user=approved_by,
            notes=notes or 'Reverted decline to approve',
        )

        actor = actor_label(approved_by)
        description = (
            f'Decline reverted and approved by {actor}. '
            f'Status changed from {previous_display} to {loan.get_status_display()}.'
        )
        if notes:
            description = f'{description} Notes: {notes.strip()}'
        log_staff_action(
            customer=loan.customer,
            loan=loan,
            user=approved_by,
            type_value='system',
            title='Decline Reverted',
            description=description,
            metadata={
                'previous_status': previous_status,
                'new_status': loan.status,
                'action': 'revert_decline',
            },
        )

        from accounts.arrive_integration import queue_decision_webhook
        queue_decision_webhook(loan, 'approved')

        return loan

    @staticmethod
    @transaction.atomic
    def update_approved_amount(loan: Loan, principal: Decimal, user=None, notes: str = '') -> Loan:
        loan = Loan.objects.select_for_update().get(pk=loan.pk)

        if loan.status not in ['ibv_pending', 'pending_signature', 'pending', 'pending_funding']:
            raise ValueError(f"Cannot update approved amount in status: {loan.status}")
        if loan.funded_payments.filter(status__in=['processing', 'completed']).exists():
            raise ValueError('Cannot update approved amount after funding has started.')

        from activity.services import actor_label, log_staff_action

        old_principal = loan.principal
        principal = LoanService.money(principal)
        if principal <= 0:
            raise ValueError('Approved amount must be greater than zero.')

        loan.principal = principal
        loan.save(update_fields=['principal', 'updated_at'])
        LoanService.rebuild_payment_schedule(loan, reprice=True)
        loan.refresh_from_db()

        actor = actor_label(user)
        detail = (
            f'Approved amount changed from ${old_principal} to ${loan.principal} by {actor}.'
        )
        if notes:
            loan.notes = ((loan.notes or '') + f"\n{notes}").strip()
            loan.save(update_fields=['notes', 'updated_at'])
            detail = f'{detail} Notes: {notes.strip()}'
        loan.log_state_event(
            event_type='amount_updated',
            previous_status=loan.status,
            new_status=loan.status,
            user=user,
            notes=detail,
        )
        log_staff_action(
            customer=loan.customer,
            loan=loan,
            user=user,
            type_value='system',
            title='Approved Amount Changed',
            description=detail,
            metadata={
                'previous_principal': str(old_principal),
                'new_principal': str(loan.principal),
                'action': 'update_approved_amount',
            },
        )

        return loan
    
    @staticmethod
    @transaction.atomic
    def fund_loan(loan: Loan, method: str = 'eft', reference: str = '', user=None) -> Loan:
        if loan.status != 'pending_funding':
            raise ValueError(f"Cannot fund loan in status: {loan.status}")
        if not loan.contract_signed:
            raise ValueError('Contract must be signed before funding.')

        from activity.services import actor_label, log_staff_action

        previous_display = loan.get_status_display()
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

        actor = actor_label(user)
        log_staff_action(
            customer=loan.customer,
            loan=loan,
            user=user,
            type_value='loan_funded',
            title='Loan Funded',
            description=(
                f'Funded by {actor} via {method.upper()}. '
                f'Status changed from {previous_display} to {loan.get_status_display()}. '
                f'Reference: {ref}.'
            ),
            metadata={'method': method, 'reference': ref, 'action': 'fund'},
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
            status__in=['pending_signature', 'pending', 'ibv_pending', 'pending_funding']
        ).order_by('-created_at').first()

        if not loan:
            raise ValueError('No application available for signature.')

        if not customer.banking_verified:
            raise ValueError('Banking verification must be completed before signing.')

        if loan.status not in ['pending_signature', 'pending_funding']:
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
    def record_payment(
        loan: Loan,
        amount: Decimal,
        payment_type: str = 'manual',
        *,
        received_date=None,
        reference: str = '',
        notes: str = '',
        user=None,
    ) -> Payment:
        """Record a received Interac/manual payment and shorten remaining PAD rows."""
        loan = Loan.objects.select_for_update().get(pk=loan.pk)
        if loan.status not in ('active', 'defaulted'):
            raise ValueError('Only collecting loans can record a received payment.')
        if payment_type not in ('manual', 'etransfer'):
            raise ValueError('Payment type must be manual or Interac e-transfer.')
        if loan.payments.filter(status='pending').exists():
            raise ValueError(
                'Cannot record a received payment while a collection is processing.'
            )

        amount = LoanService.money(amount)
        if amount <= 0:
            raise ValueError('Amount must be greater than zero.')
        balance = LoanService.money(loan.balance or Decimal('0.00'))
        if amount > balance:
            raise ValueError(
                f'Amount cannot exceed the remaining balance of ${balance}.'
            )

        received_date = received_date or timezone.localdate()
        payment = Payment.objects.create(
            loan=loan,
            amount=amount,
            type=payment_type,
            status='completed',
            scheduled_date=received_date,
            original_date=received_date,
            processed_at=timezone.now(),
            reference=reference or '',
            notes=notes or '',
            created_by=user if getattr(user, 'is_authenticated', False) else None,
        )
        loan.apply_payment(amount, user=user)
        LoanService._trim_scheduled_payments_to_balance(loan)

        from activity.services import actor_label, log_staff_action

        method_label = 'Interac' if payment_type == 'etransfer' else 'manual'
        try:
            customer = Customer.objects.get(pk=loan.customer_id)
        except Customer.DoesNotExist:
            customer = None
        if customer is not None:
            log_staff_action(
                customer=customer,
                loan=loan,
                user=user,
                type_value='payment_completed',
                title='Payment Received',
                description=(
                    f'{method_label} payment of ${amount} recorded on {received_date} '
                    f'by {actor_label(user)}.'
                ),
                metadata={
                    'action': 'record_payment',
                    'payment_id': str(payment.id),
                    'amount': str(amount),
                    'type': payment_type,
                    'received_date': str(received_date),
                },
            )
        return payment

    @staticmethod
    def _trim_scheduled_payments_to_balance(loan: Loan) -> None:
        """Drop or shrink future scheduled rows so they match remaining balance."""
        money = LoanService.money
        target = money(loan.balance or Decimal('0.00'))
        scheduled = list(
            loan.payments.filter(status='scheduled').order_by(
                '-scheduled_date', '-created_at', '-id'
            )
        )
        current = money(
            sum((money(row.amount or Decimal('0.00')) for row in scheduled), Decimal('0.00'))
        )
        extra = money(current - target)
        if extra <= 0:
            return

        for row in scheduled:
            if extra <= 0:
                break
            amount = money(row.amount or Decimal('0.00'))
            if amount <= extra:
                extra = money(extra - amount)
                row.delete()
                continue
            row.amount = money(amount - extra)
            row.save(update_fields=['amount'])
            extra = Decimal('0.00')

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
    def adjust_payment_schedule(
        loan: Loan,
        *,
        calculation_mode: str = 'payment_amount',
        payment_amount: Decimal = None,
        number_of_payments: int = None,
        frequency: str,
        start_date,
        user=None,
        notes: str = '',
        month_days=None,
    ) -> list:
        """
        Reprice the loan using the staff-selected installment amount or
        installment count and cadence, then replace scheduled payments.

        In amount mode, choose the smallest schedule where payment_amount * n
        covers principal + brokerage + daily interest for n periods. In count
        mode, calculate the daily-interest total for the selected count and
        spread the remaining balance across that count. The final installment
        is adjusted to the exact remaining balance.
        """
        if loan.status not in [
            'pending_signature',
            'pending',
            'pending_funding',
            'active',
            'defaulted',
        ]:
            raise ValueError(f"Cannot adjust schedule in status: {loan.status}")

        calculation_mode = calculation_mode or 'payment_amount'
        if calculation_mode not in ['payment_amount', 'number_of_payments']:
            raise ValueError('Invalid schedule calculation mode.')

        frequency_days = LoanService._frequency_days(frequency)
        month_days = LoanService._normalized_month_days(
            frequency,
            month_days,
        )

        principal = LoanService.money(loan.principal or Decimal('0.00'))
        if principal <= 0:
            raise ValueError('Loan principal must be greater than zero.')

        formula = LoanService.get_formula_for_amount(principal) or loan.formula
        if formula:
            amounts = LoanService.calculate_from_formula(formula, principal)
            priced_principal = amounts['total_amount']
            annual_rate = formula.annual_interest_rate
        else:
            priced_principal = principal
            annual_rate = Decimal('0.00')

        paid_total = loan.payments.filter(status='completed').aggregate(
            total=models.Sum('amount')
        )['total'] or Decimal('0.00')

        max_payments = 260
        num_payments = None
        total_amount = None
        balance_due = None

        if calculation_mode == 'number_of_payments':
            if number_of_payments is None:
                raise ValueError('Number of payments is required.')
            num_payments = int(number_of_payments)
            if num_payments < 1 or num_payments > max_payments:
                raise ValueError(f'Number of payments must be between 1 and {max_payments}.')

            total_amount = LoanService.calculate_schedule_total(
                principal_balance=priced_principal,
                annual_rate_percent=annual_rate,
                start_date=start_date,
                num_payments=num_payments,
                frequency_days=frequency_days,
            )
            balance_due = max(
                LoanService.money(total_amount - paid_total),
                Decimal('0.00'),
            )
            payment_amount = LoanService.money(balance_due / Decimal(num_payments))
        else:
            if payment_amount is None:
                raise ValueError('Payment amount is required.')
            payment_amount = LoanService.money(payment_amount)
            if payment_amount <= 0:
                raise ValueError('Payment amount must be greater than zero.')

            for candidate_count in range(1, max_payments + 1):
                candidate_total = LoanService.calculate_schedule_total(
                    principal_balance=priced_principal,
                    annual_rate_percent=annual_rate,
                    start_date=start_date,
                    num_payments=candidate_count,
                    frequency_days=frequency_days,
                )
                candidate_balance = max(
                    LoanService.money(candidate_total - paid_total),
                    Decimal('0.00'),
                )
                if payment_amount * Decimal(candidate_count) >= candidate_balance:
                    num_payments = candidate_count
                    total_amount = candidate_total
                    balance_due = candidate_balance
                    break

            if num_payments is None or total_amount is None or balance_due is None:
                minimum_payment = LoanService.money(
                    max(
                        LoanService.calculate_schedule_total(
                            principal_balance=priced_principal,
                            annual_rate_percent=annual_rate,
                            start_date=start_date,
                            num_payments=max_payments,
                            frequency_days=frequency_days,
                        ) - paid_total,
                        Decimal('0.00'),
                    ) / Decimal(max_payments)
                )
                raise ValueError(
                    f'Payment amount is too low for this schedule. '
                    f'Use at least ${minimum_payment}.'
                )

        if balance_due <= 0:
            raise ValueError('This loan has no remaining balance to schedule.')

        # Pending installments are in-flight collections. Rebuilding only deleted
        # "scheduled" rows before, which left Pending + a new schedule (duplicate
        # same-day payments and Balance After clamping to $0 twice).
        if loan.payments.filter(
            collection_attempts__status='processing'
        ).exists() or loan.payments.filter(status='pending').exists():
            raise ValueError(
                'Cannot adjust schedule while a payment collection is pending. '
                'Wait for the collection to finish or fail, then try again.'
            )

        if calculation_mode == 'number_of_payments':
            detail = (
                f'Schedule adjusted to {num_payments} {frequency} payment(s) '
                f'from {start_date}; calculated payment ${payment_amount}.'
            )
        else:
            detail = (
                f'Schedule adjusted to {num_payments} {frequency} payment(s) '
                f'from {start_date}; target payment ${payment_amount}.'
            )

        Payment.objects.filter(
            loan=loan,
            status__in=['scheduled', 'failed', 'nsf', 'unscheduled'],
        ).delete()

        loan.formula = formula
        loan.fee = LoanService.money(total_amount - principal)
        loan.total_amount = total_amount
        loan.balance = balance_due
        loan.schedule_frequency = frequency
        if month_days:
            loan.twice_monthly_day_1 = month_days[0]
            loan.twice_monthly_day_2 = month_days[1]
        else:
            loan.twice_monthly_day_1 = None
            loan.twice_monthly_day_2 = None
        loan.save(
            update_fields=[
                'formula',
                'fee',
                'total_amount',
                'balance',
                'schedule_frequency',
                'twice_monthly_day_1',
                'twice_monthly_day_2',
                'updated_at',
            ]
        )

        payments = LoanService.generate_payment_schedule(
            loan=loan,
            num_payments=num_payments,
            payment_amount=payment_amount,
            start_date=start_date,
            frequency_days=frequency_days,
            schedule_total=balance_due,
            month_days=month_days,
        )

        if notes:
            detail = f'{detail} {notes}'
        loan.log_state_event(
            event_type='amount_updated',
            previous_status=loan.status,
            new_status=loan.status,
            user=user,
            notes=detail,
        )

        from activity.services import actor_label, log_staff_action

        actor = actor_label(user)
        log_staff_action(
            customer=loan.customer,
            loan=loan,
            user=user,
            type_value='payment_scheduled',
            title='Payment Schedule Adjusted',
            description=f'{detail} By {actor}.',
            metadata={
                'action': 'adjust_schedule',
                'num_payments': num_payments,
                'frequency': frequency,
                'payment_amount': str(payment_amount),
                'start_date': str(start_date),
                'month_days': month_days or [],
            },
        )

        return payments

    @staticmethod
    def _frequency_days(frequency: str) -> int:
        frequency_days_by_key = {
            'weekly': 7,
            'bi-weekly': 14,
            'twice-monthly': 15,
            'monthly': 30,
        }
        days = frequency_days_by_key.get(frequency)
        if not days:
            raise ValueError('Invalid payment frequency.')
        return days

    @staticmethod
    def _normalized_month_days(frequency: str, month_days=None):
        if frequency != 'twice-monthly':
            return None
        days = [int(value) for value in (month_days or [])]
        unique = []
        for day in days:
            if day < 1 or day > 31:
                raise ValueError('Twice-a-month days must be between 1 and 31.')
            if day not in unique:
                unique.append(day)
        if len(unique) != 2:
            raise ValueError(
                'Twice a month requires two different days of the month.'
            )
        return sorted(unique)

    @staticmethod
    def _stored_month_days(loan: Loan):
        day_1 = getattr(loan, 'twice_monthly_day_1', None)
        day_2 = getattr(loan, 'twice_monthly_day_2', None)
        if not day_1 or not day_2:
            return None
        try:
            return LoanService._normalized_month_days(
                'twice-monthly',
                [day_1, day_2],
            )
        except ValueError:
            return None

    @staticmethod
    def _frequency_key_from_days(days: int) -> str:
        """Map a day cadence to the staff Adjust Schedule frequency choice."""
        if days <= 9:
            return 'weekly'
        if days <= 20:
            return 'bi-weekly'
        return 'monthly'

    @staticmethod
    def schedule_frequency_key(loan: Loan) -> str:
        """Selected/active payment cadence for UI."""
        stored = (getattr(loan, 'schedule_frequency', None) or '').strip()
        if stored in ('weekly', 'bi-weekly', 'monthly', 'twice-monthly'):
            return stored
        return LoanService._frequency_key_from_days(
            LoanService._schedule_frequency_days(loan)
        )

    @staticmethod
    def _protected_in_flight_payments(loan: Loan):
        """Pending collections and any installment with a processing attempt."""
        return (
            loan.payments.filter(
                models.Q(status='pending')
                | models.Q(collection_attempts__status='processing')
            )
            .distinct()
            .order_by('scheduled_date', 'created_at', 'id')
        )

    @staticmethod
    def plan_heal_upcoming_schedule_keeping_pending(
        loan: Loan,
        *,
        calculation_mode: str = 'payment_amount',
        payment_amount: Decimal = None,
        number_of_payments: int = None,
        frequency: str = 'bi-weekly',
        start_date=None,
        reprice: bool = False,
        month_days=None,
    ) -> dict:
        """Simulate rebuilding Scheduled/failed/nsf rows while keeping Pending.

        Does not write. Remaining to schedule =
        loan.total_amount (or repriced total) − completed − protected in-flight.
        Interest for the keep-total path is already baked into total_amount;
        ``reprice=True`` uses the same daily-interest math as Adjust Schedule.
        """
        if loan.status not in [
            'pending_signature',
            'pending',
            'pending_funding',
            'active',
            'defaulted',
        ]:
            raise ValueError(f'Cannot heal schedule in status: {loan.status}')

        calculation_mode = calculation_mode or 'payment_amount'
        if calculation_mode not in ['payment_amount', 'number_of_payments']:
            raise ValueError('Invalid schedule calculation mode.')

        money = LoanService.money
        frequency_days = LoanService._frequency_days(frequency)
        if frequency == 'twice-monthly':
            month_days = LoanService._normalized_month_days(
                frequency,
                month_days or LoanService._stored_month_days(loan),
            )
        else:
            month_days = None
        principal = money(loan.principal or Decimal('0.00'))
        if principal <= 0:
            raise ValueError('Loan principal must be greater than zero.')

        protected = list(LoanService._protected_in_flight_payments(loan))
        protected_ids = {p.id for p in protected}
        completed = list(
            loan.payments.filter(status='completed').order_by(
                'scheduled_date', 'created_at', 'id'
            )
        )
        replaceable = list(
            loan.payments.filter(status__in=['scheduled', 'failed', 'nsf', 'unscheduled'])
            .exclude(id__in=protected_ids)
            .order_by('scheduled_date', 'created_at', 'id')
        )

        completed_sum = sum(
            (money(p.amount or Decimal('0.00')) for p in completed),
            Decimal('0.00'),
        )
        protected_sum = sum(
            (money(p.amount or Decimal('0.00')) for p in protected),
            Decimal('0.00'),
        )
        reserved = money(completed_sum + protected_sum)

        formula = LoanService.get_formula_for_amount(principal) or loan.formula
        if start_date is None:
            if protected:
                start_date = protected[-1].scheduled_date + timedelta(days=frequency_days)
            else:
                start_date = timezone.localdate()
            if start_date < timezone.localdate():
                start_date = timezone.localdate()

        max_payments = 260
        total_amount = money(loan.total_amount or Decimal('0.00'))
        annual_rate = Decimal('0.00')
        priced_principal = principal

        if reprice:
            if formula:
                amounts = LoanService.calculate_from_formula(formula, principal)
                priced_principal = amounts['total_amount']
                annual_rate = formula.annual_interest_rate
            else:
                priced_principal = principal
                annual_rate = Decimal('0.00')

        if calculation_mode == 'number_of_payments':
            if number_of_payments is None:
                raise ValueError('Number of payments is required.')
            num_payments = int(number_of_payments)
            if num_payments < 1 or num_payments > max_payments:
                raise ValueError(
                    f'Number of payments must be between 1 and {max_payments}.'
                )
            if reprice:
                total_amount = LoanService.calculate_schedule_total(
                    principal_balance=priced_principal,
                    annual_rate_percent=annual_rate,
                    start_date=start_date,
                    num_payments=num_payments,
                    frequency_days=frequency_days,
                )
            schedule_total = max(money(total_amount - reserved), Decimal('0.00'))
            installment = money(schedule_total / Decimal(num_payments)) if schedule_total else Decimal('0.00')
        else:
            if payment_amount is None:
                # Prefer a typical open scheduled amount, else formula default split.
                sample = next(
                    (
                        p.amount
                        for p in replaceable
                        if p.status == 'scheduled'
                        and money(p.amount or 0) > Decimal('1.00')
                    ),
                    None,
                )
                if sample is not None:
                    payment_amount = money(sample)
                elif formula and int(formula.default_number_of_payments or 0) > 0:
                    payment_amount = money(
                        max(money(total_amount - reserved), Decimal('0.00'))
                        / Decimal(int(formula.default_number_of_payments))
                    )
                else:
                    raise ValueError('Payment amount is required.')
            payment_amount = money(payment_amount)
            if payment_amount <= 0:
                raise ValueError('Payment amount must be greater than zero.')

            if reprice:
                num_payments = None
                for candidate_count in range(1, max_payments + 1):
                    candidate_total = LoanService.calculate_schedule_total(
                        principal_balance=priced_principal,
                        annual_rate_percent=annual_rate,
                        start_date=start_date,
                        num_payments=candidate_count,
                        frequency_days=frequency_days,
                    )
                    candidate_schedule = max(
                        money(candidate_total - reserved),
                        Decimal('0.00'),
                    )
                    if payment_amount * Decimal(candidate_count) >= candidate_schedule:
                        num_payments = candidate_count
                        total_amount = candidate_total
                        schedule_total = candidate_schedule
                        break
                if num_payments is None:
                    raise ValueError(
                        'Payment amount is too low for this remaining schedule.'
                    )
                installment = payment_amount
            else:
                schedule_total = max(money(total_amount - reserved), Decimal('0.00'))
                if schedule_total <= 0:
                    num_payments = 0
                    installment = Decimal('0.00')
                else:
                    num_payments = int(
                        (schedule_total / payment_amount).to_integral_value(
                            rounding=ROUND_HALF_UP
                        )
                    )
                    if num_payments < 1:
                        num_payments = 1
                    if payment_amount * Decimal(num_payments) < schedule_total:
                        num_payments += 1
                    num_payments = min(num_payments, max_payments)
                    installment = payment_amount

        if schedule_total < 0:
            schedule_total = Decimal('0.00')

        proposed = []
        remaining = schedule_total
        unadjusted_dates = list(
            business_calendar.iter_unadjusted_dates(
                start_date,
                num_payments,
                frequency_days,
                month_days=month_days,
            )
        )
        holidays = business_calendar.holiday_dates_for_years(
            {value.year for value in unadjusted_dates} or {start_date.year}
        )
        for index in range(num_payments):
            if remaining <= 0:
                break
            is_last = index == num_payments - 1
            amount = remaining if is_last else min(installment, remaining)
            amount = money(amount)
            if amount <= 0:
                break
            date_fields = business_calendar.payment_date_fields(
                unadjusted_dates[index], holidays=holidays
            )
            proposed.append(
                {
                    'original_date': date_fields['original_date'],
                    'scheduled_date': date_fields['scheduled_date'],
                    'amount': amount,
                    'status': 'scheduled',
                }
            )
            remaining = money(remaining - amount)

        def _row(payment):
            return {
                'id': str(payment.id),
                'scheduled_date': payment.scheduled_date,
                'amount': money(payment.amount or Decimal('0.00')),
                'status': payment.status,
            }

        open_after = money(protected_sum + sum((p['amount'] for p in proposed), Decimal('0.00')))
        return {
            'loan_id': str(loan.id),
            'loan_status': loan.status,
            'reprice': reprice,
            'frequency': frequency,
            'frequency_days': frequency_days,
            'calculation_mode': calculation_mode,
            'start_date': start_date,
            'payment_amount': money(installment),
            'num_payments': len(proposed),
            'total_amount_before': money(loan.total_amount or Decimal('0.00')),
            'total_amount_after': money(total_amount),
            'balance_before': money(loan.balance or Decimal('0.00')),
            'balance_after': money(total_amount - completed_sum),
            'completed_sum': completed_sum,
            'protected_sum': protected_sum,
            'schedule_total': schedule_total,
            'open_sum_after': open_after,
            'protected': [_row(p) for p in protected],
            'will_delete': [_row(p) for p in replaceable],
            'proposed': proposed,
            'completed': [_row(p) for p in completed],
        }

    @staticmethod
    @transaction.atomic
    def heal_upcoming_schedule_keeping_pending(
        loan: Loan,
        *,
        calculation_mode: str = 'payment_amount',
        payment_amount: Decimal = None,
        number_of_payments: int = None,
        frequency: str = 'bi-weekly',
        start_date=None,
        reprice: bool = False,
        dry_run: bool = True,
        user=None,
        notes: str = '',
        month_days=None,
    ) -> dict:
        """Rebuild upcoming schedule rows; never modify Pending / in-flight payments.

        dry_run=True (default) only returns the simulation plan.
        """
        plan = LoanService.plan_heal_upcoming_schedule_keeping_pending(
            loan,
            calculation_mode=calculation_mode,
            payment_amount=payment_amount,
            number_of_payments=number_of_payments,
            frequency=frequency,
            start_date=start_date,
            reprice=reprice,
            month_days=month_days,
        )
        plan['dry_run'] = dry_run
        if dry_run:
            return plan

        loan = Loan.objects.select_for_update().get(pk=loan.pk)
        # Recompute under lock so apply matches current DB.
        plan = LoanService.plan_heal_upcoming_schedule_keeping_pending(
            loan,
            calculation_mode=calculation_mode,
            payment_amount=payment_amount,
            number_of_payments=number_of_payments,
            frequency=frequency,
            start_date=start_date,
            reprice=reprice,
            month_days=month_days,
        )
        plan['dry_run'] = False

        protected_ids = [p['id'] for p in plan['protected']]
        delete_qs = loan.payments.filter(status__in=['scheduled', 'failed', 'nsf', 'unscheduled'])
        if protected_ids:
            delete_qs = delete_qs.exclude(id__in=protected_ids)
        delete_qs.delete()

        if reprice:
            principal = LoanService.money(loan.principal or Decimal('0.00'))
            loan.formula = LoanService.get_formula_for_amount(principal) or loan.formula
            loan.total_amount = plan['total_amount_after']
            loan.fee = LoanService.money(plan['total_amount_after'] - principal)
            loan.balance = plan['balance_after']
            loan.save(
                update_fields=['formula', 'fee', 'total_amount', 'balance', 'updated_at']
            )
        else:
            # Keep priced totals; only refresh balance to total − completed.
            loan.balance = plan['balance_after']
            loan.save(update_fields=['balance', 'updated_at'])

        created = []
        for row in plan['proposed']:
            created.append(
                Payment.objects.create(
                    loan=loan,
                    amount=row['amount'],
                    scheduled_date=row['scheduled_date'],
                    original_date=row.get('original_date') or row['scheduled_date'],
                    type='scheduled',
                    status='scheduled',
                )
            )

        from activity.services import actor_label, log_staff_action

        actor = actor_label(user)
        detail = (
            f'Upcoming schedule healed by {actor}: kept {len(plan["protected"])} '
            f'in-flight payment(s), removed {len(plan["will_delete"])} open row(s), '
            f'created {len(created)} scheduled payment(s) totaling '
            f'${plan["schedule_total"]} from {plan["start_date"]}.'
        )
        if notes:
            detail = f'{detail} {notes}'
        log_staff_action(
            customer=loan.customer,
            loan=loan,
            user=user,
            type_value='payment_scheduled',
            title='Upcoming Schedule Healed',
            description=detail,
            metadata={
                'action': 'heal_upcoming_schedule_keeping_pending',
                'protected_payment_ids': list(protected_ids),
                'num_created': len(created),
                'schedule_total': str(plan['schedule_total']),
                'reprice': reprice,
            },
        )
        plan['created_ids'] = [str(p.id) for p in created]
        return plan

    @staticmethod
    @transaction.atomic
    def generate_payment_schedule(loan: Loan, num_payments: int,
                                  payment_amount: Decimal,
                                  start_date,
                                  frequency_days: int = 14,
                                  schedule_total: Decimal = None,
                                  month_days=None) -> list:
        """Generate a payment schedule for a loan."""
        payments = []
        remaining = LoanService.money(schedule_total or loan.total_amount)
        unadjusted_dates = list(
            business_calendar.iter_unadjusted_dates(
                start_date, num_payments, frequency_days, month_days=month_days
            )
        )
        holidays = business_calendar.holiday_dates_for_years(
            {value.year for value in unadjusted_dates} or {start_date.year}
        )

        for i, original_date in enumerate(unadjusted_dates):
            is_last = (i == num_payments - 1)
            amt = remaining if is_last else min(payment_amount, remaining)
            remaining -= amt
            date_fields = business_calendar.payment_date_fields(
                original_date, holidays=holidays
            )
            payment = Payment.objects.create(
                loan=loan,
                amount=amt,
                type='scheduled',
                status='scheduled',
                **date_fields,
            )
            payments.append(payment)

        return payments

    @staticmethod
    def rebalance_open_schedule_to_total(loan: Loan, *, protect_payment_id=None) -> None:
        """Keep open installments aligned with loan.total_amount after a manual edit.

        Overschedule (e.g. balloon one payment without lowering the rest) causes
        multiple trailing Balance After $0 rows. Reduce/remove later scheduled
        installments; never touch pending/in-flight or deferral-fee rows.
        """
        money = LoanService.money
        payments = list(
            loan.payments.exclude(status='cancelled').order_by(
                'scheduled_date', 'created_at', 'id'
            )
        )
        completed_sum = sum(
            (money(p.amount or Decimal('0.00')) for p in payments if p.status == 'completed'),
            Decimal('0.00'),
        )
        target_open = money((loan.total_amount or Decimal('0.00')) - completed_sum)
        if target_open < 0:
            target_open = Decimal('0.00')

        open_payments = [
            p for p in payments if p.status in ('scheduled', 'pending', 'failed', 'nsf')
        ]
        open_sum = sum(
            (money(p.amount or Decimal('0.00')) for p in open_payments),
            Decimal('0.00'),
        )
        delta = money(open_sum - target_open)
        # Only correct overshoot (ballooned installment / leftover stubs).
        # Underschedule is left alone — staff use Adjust Schedule to rebuild.
        if delta <= 0:
            return

        for payment in reversed(open_payments):
            if delta <= 0:
                break
            if protect_payment_id and payment.id == protect_payment_id:
                continue
            if payment.status != 'scheduled':
                continue
            if LoanService.is_non_installment_fee_payment(payment):
                continue
            amount = money(payment.amount or Decimal('0.00'))
            if amount <= delta:
                delta = money(delta - amount)
                payment.delete()
            else:
                payment.amount = money(amount - delta)
                payment.save(update_fields=['amount'])
                delta = Decimal('0.00')

    @staticmethod
    @transaction.atomic
    def update_scheduled_payment(
        payment: Payment,
        *,
        scheduled_date=None,
        amount: Decimal = None,
        user=None,
    ) -> Payment:
        """Edit one open installment's date and/or amount without rebuilding the schedule."""
        payment = Payment.objects.select_for_update().select_related('loan').get(
            pk=payment.pk
        )
        loan = Loan.objects.select_for_update().get(pk=payment.loan_id)

        if loan.status not in [
            'pending_signature',
            'pending',
            'pending_funding',
            'active',
            'defaulted',
        ]:
            raise ValueError(f'Cannot edit payments for loan in status: {loan.status}')

        if payment.status not in ('scheduled', 'pending', 'failed', 'nsf'):
            raise ValueError('Only open schedule installments can be edited.')

        if payment.collection_attempts.filter(status__in=['processing', 'completed']).exists():
            raise ValueError(
                'This installment has an active collection and cannot be edited.'
            )

        if scheduled_date is None and amount is None:
            raise ValueError('Select modify date and/or modify payment before saving.')

        previous_date = payment.scheduled_date
        previous_amount = payment.amount
        update_fields = []
        amount_changed = False

        if scheduled_date is not None and scheduled_date != payment.scheduled_date:
            date_fields = business_calendar.payment_date_fields(scheduled_date)
            payment.original_date = date_fields['original_date']
            payment.scheduled_date = date_fields['scheduled_date']
            update_fields.extend(['original_date', 'scheduled_date'])

        if amount is not None:
            amount = LoanService.money(amount)
            if amount <= 0:
                raise ValueError('Payment amount must be greater than zero.')
            if amount != payment.amount:
                payment.amount = amount
                update_fields.append('amount')
                amount_changed = True

        if not update_fields:
            return payment

        # Failed/NSF rows that staff re-date or re-amount become collectible again.
        if payment.status in ('failed', 'nsf'):
            payment.status = 'scheduled'
            payment.failure_reason = None
            payment.processed_at = None
            update_fields.extend(['status', 'failure_reason', 'processed_at'])

        payment.save(update_fields=list(dict.fromkeys(update_fields)))

        if amount_changed:
            LoanService.rebalance_open_schedule_to_total(
                loan,
                protect_payment_id=payment.id,
            )
            payment.refresh_from_db()

        from activity.services import actor_label, log_staff_action

        actor = actor_label(user)
        changes = []
        if 'scheduled_date' in update_fields:
            changes.append(f'date {previous_date} → {payment.scheduled_date}')
        if 'amount' in update_fields:
            changes.append(f'amount ${previous_amount} → ${payment.amount}')
        detail = f'Schedule installment updated by {actor}: {", ".join(changes)}.'
        try:
            customer = Customer.objects.get(pk=loan.customer_id)
        except Exception:
            customer = None
        if customer is not None:
            log_staff_action(
                customer=customer,
                loan=loan,
                user=user,
                type_value='payment_scheduled',
                title='Payment Installment Updated',
                description=detail,
                metadata={
                    'action': 'update_scheduled_payment',
                    'payment_id': str(payment.id),
                    'previous_date': str(previous_date),
                    'new_date': str(payment.scheduled_date),
                    'previous_amount': str(previous_amount),
                    'new_amount': str(payment.amount),
                    'original_amount': str(previous_amount),
                },
            )
        return payment

    DEFERRAL_FEE_AMOUNT = Decimal('35.00')
    DEFERRAL_FEE_NOTE = 'Deferral fee $35'
    COLLECTION_FAILURE_FEE_AMOUNT = Decimal('50.00')
    COLLECTION_FAILURE_FEE_NOTE = 'Collection failure fee $50'
    COLLECTION_FAILURE_RECOVERY_NOTE = 'Failed collection recovery'
    COLLECTION_FAILURE_INTEREST_NOTE = 'Collection failure daily interest'
    COLLECTION_FAILURE_ID_RE = re.compile(r'Collection failure id:\s*([0-9a-fA-F-]{36})')

    @staticmethod
    def is_deferral_fee_payment(payment: Payment) -> bool:
        notes = (payment.notes or '').strip()
        return (
            payment.amount == LoanService.DEFERRAL_FEE_AMOUNT
            and notes.startswith('Deferral fee')
        )

    @staticmethod
    def is_collection_failure_fee_payment(payment: Payment) -> bool:
        notes = (payment.notes or '').strip()
        return notes.startswith(LoanService.COLLECTION_FAILURE_FEE_NOTE)

    @staticmethod
    def is_collection_failure_recovery_payment(payment: Payment) -> bool:
        notes = (payment.notes or '').strip()
        return notes.startswith(LoanService.COLLECTION_FAILURE_RECOVERY_NOTE)

    @staticmethod
    def is_collection_failure_interest_payment(payment: Payment) -> bool:
        notes = (payment.notes or '').strip()
        return notes.startswith(LoanService.COLLECTION_FAILURE_INTEREST_NOTE)

    @staticmethod
    def collection_failure_interest_amount(payment: Payment) -> Decimal:
        for line in (payment.notes or '').splitlines():
            if not line.startswith('Extension interest: $'):
                continue
            try:
                return LoanService.money(Decimal(line.split('$', 1)[1]))
            except Exception:
                return Decimal('0.00')
        return Decimal('0.00')

    @staticmethod
    def _collection_failure_original_fill_plan(payments, *, amount: Decimal, cap: Decimal):
        """Top up original remainder installments to the regular cap.

        Returns leftover extra and ``(payment, new_amount, addition)`` updates.
        Only scheduled original rows are changed so failed history, pending
        collections, and generated recovery/fee rows stay intact.
        """
        money = LoanService.money
        remaining = money(amount)
        cap = money(cap or Decimal('0.00'))
        updates = []
        if remaining <= 0 or cap <= 0:
            return remaining, updates

        originals = [
            payment
            for payment in payments
            if payment.status == 'scheduled'
            and not LoanService.is_non_installment_fee_payment(payment)
        ]
        for payment in reversed(originals):
            current = money(payment.amount or Decimal('0.00'))
            space = money(max(cap - current, Decimal('0.00')))
            if space <= 0:
                continue
            addition = min(remaining, space)
            updates.append((payment, money(current + addition), addition))
            remaining = money(remaining - addition)
            if remaining <= 0:
                break
        return remaining, updates

    @staticmethod
    def _collection_failure_original_cap_space(payments, cap: Decimal) -> Decimal:
        money = LoanService.money
        cap = money(cap or Decimal('0.00'))
        if cap <= 0:
            return Decimal('0.00')
        space = Decimal('0.00')
        for payment in payments:
            if payment.status != 'scheduled':
                continue
            if LoanService.is_non_installment_fee_payment(payment):
                continue
            current = money(payment.amount or Decimal('0.00'))
            if current < cap:
                space += cap - current
        return money(space)

    @staticmethod
    def _collection_failure_payment_cap(
        loan: Loan,
        failed_payment: Payment | None,
        missed_amount: Decimal,
    ) -> Decimal:
        money = LoanService.money
        if (
            failed_payment is not None
            and (failed_payment.amount or Decimal('0.00')) > 0
        ):
            return money(failed_payment.amount)
        if missed_amount > 0:
            return money(missed_amount)
        for payment in loan.payments.order_by('scheduled_date', 'created_at', 'id'):
            if LoanService.is_non_installment_fee_payment(payment):
                continue
            if payment.amount and payment.amount > 0:
                return money(payment.amount)
        return Decimal('0.00')

    @staticmethod
    def _allocate_collection_failure_extra(
        loan: Loan,
        *,
        amount: Decimal,
        cap: Decimal,
        first_bucket_date,
        frequency_days: int,
        failure_note: str,
    ) -> Payment | None:
        money = LoanService.money
        remaining = money(amount)
        if remaining <= 0:
            return None

        originals = list(
            loan.payments.exclude(status__in=['completed', 'cancelled', 'failed', 'nsf'])
            .order_by('scheduled_date', 'created_at', 'id')
        )
        remaining, original_updates = LoanService._collection_failure_original_fill_plan(
            originals,
            amount=remaining,
            cap=cap,
        )
        for payment, new_amount, _addition in original_updates:
            payment.amount = new_amount
            payment.save(update_fields=['amount'])
        if remaining <= 0:
            return original_updates[-1][0] if original_updates else None

        buckets = list(
            loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
            ).order_by('scheduled_date', 'created_at', 'id')
        )
        touched_bucket = None

        if cap <= 0:
            cap = remaining

        if buckets:
            active_bucket = buckets[-1]
            current_amount = money(active_bucket.amount or Decimal('0.00'))
            space = money(max(cap - current_amount, Decimal('0.00')))
            if space > 0:
                addition = min(remaining, space)
                active_bucket.amount = money(current_amount + addition)
                active_bucket.notes = (
                    f'{(active_bucket.notes or "").strip()}\n{failure_note}'
                ).strip()
                active_bucket.save(update_fields=['amount', 'notes'])
                touched_bucket = active_bucket
                remaining = money(remaining - addition)
            next_date = (active_bucket.scheduled_date or first_bucket_date) + timedelta(
                days=frequency_days
            )
        else:
            next_date = first_bucket_date

        while remaining > 0:
            bucket_amount = min(remaining, cap)
            unadjusted_date = next_date
            date_fields = business_calendar.payment_date_fields(unadjusted_date)
            touched_bucket = Payment.objects.create(
                loan=loan,
                amount=bucket_amount,
                type='scheduled',
                status='scheduled',
                notes=(
                    f'{LoanService.COLLECTION_FAILURE_FEE_NOTE}\n'
                    f'{failure_note}'
                ),
                **date_fields,
            )
            remaining = money(remaining - bucket_amount)
            next_date = unadjusted_date + timedelta(days=frequency_days)

        return touched_bucket

    @staticmethod
    def _collection_failure_ids_from_payment(payment: Payment) -> set[str]:
        return set(
            LoanService.COLLECTION_FAILURE_ID_RE.findall((payment.notes or '').strip())
        )

    @staticmethod
    @transaction.atomic
    def rebuild_collection_failure_schedule(
        loan: Loan,
        *,
        dry_run: bool = True,
    ) -> dict:
        """Rebuild old generated collection-failure rows into capped fee buckets."""
        # Keep the row lock off nullable joins; Postgres rejects FOR UPDATE on
        # the nullable side of an outer join.
        loan = Loan.objects.select_for_update().get(pk=loan.pk)
        money = LoanService.money
        frequency_days = LoanService._schedule_frequency_days(loan)

        generated_payments = list(
            loan.payments.filter(
                models.Q(notes__startswith=LoanService.COLLECTION_FAILURE_RECOVERY_NOTE)
                | models.Q(notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE)
                | models.Q(notes__startswith=LoanService.COLLECTION_FAILURE_INTEREST_NOTE)
            ).order_by('scheduled_date', 'created_at', 'id')
        )
        legacy_interest_payments = [
            payment
            for payment in generated_payments
            if LoanService.is_collection_failure_interest_payment(payment)
        ]

        collection_ids = set()
        interest_by_collection_id: dict[str, Decimal] = {}
        for payment in generated_payments:
            ids = LoanService._collection_failure_ids_from_payment(payment)
            collection_ids.update(ids)
            if LoanService.is_collection_failure_interest_payment(payment):
                interest_amount = LoanService.collection_failure_interest_amount(payment)
                for collection_id in ids:
                    interest_by_collection_id[collection_id] = money(
                        interest_by_collection_id.get(collection_id, Decimal('0.00'))
                        + interest_amount
                    )
            elif LoanService.is_collection_failure_fee_payment(payment):
                interest_amount = LoanService.collection_failure_interest_amount(payment)
                for collection_id in ids:
                    if collection_id not in interest_by_collection_id:
                        interest_by_collection_id[collection_id] = interest_amount

        collections = list(
            loan.collection_payments.select_related('payment')
            .filter(
                models.Q(id__in=collection_ids)
                | models.Q(status__in=['failed', 'returned'], payment__status__in=['failed', 'nsf'])
            )
            .order_by('payment__scheduled_date', 'initiated_at', 'created_at', 'id')
        )

        non_generated_payments = list(
            loan.payments.exclude(pk__in=[payment.pk for payment in generated_payments])
            .exclude(status='cancelled')
            .order_by('scheduled_date', 'created_at', 'id')
        )
        last_original_date = None
        for payment in non_generated_payments:
            if payment.scheduled_date and (
                last_original_date is None or payment.scheduled_date > last_original_date
            ):
                last_original_date = payment.scheduled_date
        if last_original_date is None:
            last_original_date = timezone.localdate()

        recovery_date = last_original_date + timedelta(days=frequency_days)
        bucket_date = recovery_date + timedelta(days=frequency_days)
        cap = Decimal('0.00')
        for collection in collections:
            amount = money(collection.payment.amount if collection.payment else collection.amount)
            cap = max(cap, amount)
        if cap <= 0:
            cap = Decimal('0.00')
        has_over_cap_fee_bucket = any(
            LoanService.is_collection_failure_fee_payment(payment)
            and cap > 0
            and money(payment.amount or Decimal('0.00')) > cap
            for payment in generated_payments
        )
        has_missing_generated_rows = bool(collections) and not generated_payments
        has_underfilled_original = (
            bool(collections)
            and LoanService._collection_failure_original_cap_space(
                non_generated_payments,
                cap,
            )
            > 0
        )
        if (
            not legacy_interest_payments
            and not has_over_cap_fee_bucket
            and not has_missing_generated_rows
            and not has_underfilled_original
        ):
            return {
                'loan_id': str(loan.id),
                'dry_run': dry_run,
                'frequency_days': frequency_days,
                'collections_count': 0,
                'delete_count': 0,
                'create_count': 0,
                'existing_generated_total': money(
                    sum(
                        (
                            money(payment.amount or Decimal('0.00'))
                            for payment in generated_payments
                        ),
                        Decimal('0.00'),
                    )
                ),
                'new_generated_total': Decimal('0.00'),
                'balance_before': money(loan.balance or Decimal('0.00')),
                'balance_after': money(loan.balance or Decimal('0.00')),
                'total_amount_before': money(loan.total_amount or Decimal('0.00')),
                'total_amount_after': money(loan.total_amount or Decimal('0.00')),
                'fee_before': money(loan.fee or Decimal('0.00')),
                'fee_after': money(loan.fee or Decimal('0.00')),
                'will_delete': [],
                'proposed': [],
                'skipped_reason': 'already_current_shape',
            }

        proposed = []
        bucket_amount = Decimal('0.00')
        current_bucket_date = bucket_date
        bucket_notes: list[str] = []
        simulated_balance = money(loan.balance or Decimal('0.00'))
        original_amounts_before = {
            payment.id: payment.amount
            for payment in non_generated_payments
        }
        original_amount_updates = {}

        def flush_bucket():
            nonlocal bucket_amount, current_bucket_date, bucket_notes
            if bucket_amount <= 0:
                return
            proposed.append({
                'amount': money(bucket_amount),
                'status': 'scheduled',
                'type': 'scheduled',
                'scheduled_date': current_bucket_date,
                'notes': (
                    f'{LoanService.COLLECTION_FAILURE_FEE_NOTE}\n'
                    + '\n'.join(bucket_notes)
                ),
                'kind': 'fee_interest',
            })
            bucket_amount = Decimal('0.00')
            bucket_notes = []
            current_bucket_date = current_bucket_date + timedelta(days=frequency_days)

        for collection in collections:
            collection_id = str(collection.id)
            reason = (
                collection.failure_reason
                or (collection.payment.failure_reason if collection.payment else '')
                or 'Unknown'
            ).strip()
            missed_amount = money(
                collection.payment.amount if collection.payment else collection.amount
            )
            if missed_amount > 0:
                proposed.append({
                    'amount': missed_amount,
                    'status': 'scheduled',
                    'type': 'scheduled',
                    'scheduled_date': recovery_date,
                    'notes': (
                        f'{LoanService.COLLECTION_FAILURE_RECOVERY_NOTE} ${missed_amount}\n'
                        f'Collection failure id: {collection_id}\n'
                        f'Reason: {reason}'
                    ),
                    'kind': 'recovery',
                })

            stored_interest = collection_id in interest_by_collection_id
            if stored_interest:
                interest = money(interest_by_collection_id[collection_id])
            else:
                interest = LoanService._period_interest_for_balance(
                    loan,
                    outstanding=simulated_balance,
                    days=frequency_days * 2,
                )
            extra = money(LoanService.COLLECTION_FAILURE_FEE_AMOUNT + interest)
            simulated_balance = money(simulated_balance + extra)
            remaining, fill_updates = LoanService._collection_failure_original_fill_plan(
                non_generated_payments,
                amount=extra,
                cap=cap,
            )
            for payment, new_amount, _addition in fill_updates:
                payment.amount = new_amount
                original_amount_updates[payment.id] = payment
            note = (
                f'Collection failure id: {collection_id}\n'
                f'Reason: {reason}\n'
                f'NSF fee: ${LoanService.COLLECTION_FAILURE_FEE_AMOUNT}\n'
                f'Extension interest: ${interest}'
            )
            while remaining > 0:
                if cap <= 0:
                    addition = remaining
                else:
                    space = money(cap - bucket_amount)
                    if space <= 0:
                        flush_bucket()
                        space = cap
                    addition = min(remaining, space)

                bucket_amount = money(bucket_amount + addition)
                if note not in bucket_notes:
                    bucket_notes.append(note)
                remaining = money(remaining - addition)

        flush_bucket()

        existing_generated_total = sum(
            (money(payment.amount or Decimal('0.00')) for payment in generated_payments),
            Decimal('0.00'),
        )
        new_generated_total = money(
            sum((row['amount'] for row in proposed), Decimal('0.00'))
        )
        completed_sum = sum(
            (
                money(payment.amount or Decimal('0.00'))
                for payment in non_generated_payments
                if payment.status == 'completed'
            ),
            Decimal('0.00'),
        )
        open_original_sum = sum(
            (
                money(payment.amount or Decimal('0.00'))
                for payment in non_generated_payments
                if payment.status not in ('completed', 'cancelled', 'failed', 'nsf')
            ),
            Decimal('0.00'),
        )
        balance_after = money(open_original_sum + new_generated_total)
        total_after = money(completed_sum + balance_after)
        fee_after = money(total_after - money(loan.principal or Decimal('0.00')))

        plan = {
            'loan_id': str(loan.id),
            'dry_run': dry_run,
            'frequency_days': frequency_days,
            'collections_count': len(collections),
            'delete_count': len(generated_payments),
            'create_count': len(proposed),
            'existing_generated_total': money(existing_generated_total),
            'new_generated_total': new_generated_total,
            'balance_before': money(loan.balance or Decimal('0.00')),
            'balance_after': balance_after,
            'total_amount_before': money(loan.total_amount or Decimal('0.00')),
            'total_amount_after': total_after,
            'fee_before': money(loan.fee or Decimal('0.00')),
            'fee_after': fee_after,
            'will_delete': [
                {
                    'id': str(payment.id),
                    'scheduled_date': payment.scheduled_date,
                    'amount': money(payment.amount or Decimal('0.00')),
                    'status': payment.status,
                    'notes': (payment.notes or '').splitlines()[0] if payment.notes else '',
                }
                for payment in generated_payments
            ],
            'proposed': proposed,
        }

        if dry_run or not collections:
            for payment in non_generated_payments:
                payment.amount = original_amounts_before[payment.id]
            return plan

        for payment in original_amount_updates.values():
            payment.save(update_fields=['amount'])
        for payment in generated_payments:
            payment.delete()
        for row in proposed:
            date_fields = business_calendar.payment_date_fields(row['scheduled_date'])
            Payment.objects.create(
                loan=loan,
                amount=row['amount'],
                type=row['type'],
                status=row['status'],
                notes=row['notes'],
                **date_fields,
            )

        loan.balance = balance_after
        loan.total_amount = total_after
        loan.fee = fee_after
        loan.save(update_fields=['balance', 'total_amount', 'fee', 'updated_at'])
        if loan.status == 'stopped':
            loan.unschedule_remaining_payments()
        return plan

    @staticmethod
    def is_non_installment_fee_payment(payment: Payment) -> bool:
        return (
            LoanService.is_deferral_fee_payment(payment)
            or LoanService.is_collection_failure_fee_payment(payment)
            or LoanService.is_collection_failure_recovery_payment(payment)
            or LoanService.is_collection_failure_interest_payment(payment)
        )

    @staticmethod
    def _schedule_frequency_days(loan: Loan) -> int:
        """Cadence for deferral placement and frequency badge (selected schedule).

        Prefer the staff-selected cadence stored on the loan, then gaps between
        real installments, then formula default, then bi-weekly.
        """
        stored = (getattr(loan, 'schedule_frequency', None) or '').strip()
        if stored:
            try:
                return LoanService._frequency_days(stored)
            except ValueError:
                pass
        dates = []
        for payment in loan.payments.order_by('scheduled_date', 'created_at', 'id'):
            if LoanService.is_non_installment_fee_payment(payment):
                continue
            if payment.scheduled_date is None:
                continue
            if not dates or payment.scheduled_date != dates[-1]:
                dates.append(payment.scheduled_date)
        if len(dates) >= 2:
            for index in range(len(dates) - 1):
                delta = (dates[index + 1] - dates[index]).days
                if delta > 0:
                    return delta

        formula = getattr(loan, 'formula', None)
        if formula and getattr(formula, 'default_frequency_days', None):
            days = int(formula.default_frequency_days)
            if days > 0:
                return days
        return 14

    @staticmethod
    def _deferral_extra_interest(loan: Loan, frequency_days: int) -> Decimal:
        """Interest for extending the schedule by one payment period.

        Uses the same annualized daily rate as Adjust Schedule
        (``repayment_percent / 100 / 365``) on the current outstanding balance.
        Falls back to flat daily interest from the fee breakdown when no rate.
        """
        if frequency_days <= 0:
            return Decimal('0.00')

        money = LoanService.money
        formula = getattr(loan, 'formula', None)
        outstanding = money(loan.balance or Decimal('0.00'))

        if formula is not None:
            annual_rate = formula.annual_interest_rate or Decimal('0.00')
            if annual_rate > 0 and outstanding > 0:
                daily_rate = annual_rate / Decimal('100') / Decimal('365')
                return money(outstanding * daily_rate * Decimal(frequency_days))

        principal = loan.principal or Decimal('0.00')
        brokerage = Decimal('0.00')
        if formula is not None and principal:
            brokerage = money(principal * formula.brokerage_percent / Decimal('100'))

        deferral_fee_total = Decimal('0.00')
        for payment in loan.payments.all():
            if LoanService.is_non_installment_fee_payment(payment):
                deferral_fee_total += money(payment.amount or Decimal('0.00'))

        planned_interest = money(
            max(
                (loan.fee or Decimal('0.00')) - brokerage - deferral_fee_total,
                Decimal('0.00'),
            )
        )
        if formula is not None:
            planned_days = (
                int(formula.default_number_of_payments)
                * int(formula.default_frequency_days)
            )
        else:
            schedule_dates = list(
                loan.payments.order_by('scheduled_date').values_list(
                    'scheduled_date', flat=True
                )
            )
            if len(schedule_dates) >= 2:
                planned_days = max((schedule_dates[-1] - schedule_dates[0]).days, 0)
            else:
                planned_days = 0

        if planned_days <= 0 or planned_interest <= 0:
            return Decimal('0.00')
        daily_interest = planned_interest / Decimal(planned_days)
        return money(daily_interest * Decimal(frequency_days))

    @staticmethod
    def _period_interest_for_balance(
        loan: Loan,
        *,
        outstanding: Decimal,
        days: int,
    ) -> Decimal:
        if days <= 0:
            return Decimal('0.00')
        money = LoanService.money
        formula = getattr(loan, 'formula', None)
        annual_rate = (
            formula.annual_interest_rate
            if formula is not None
            else Decimal('0.00')
        )
        if annual_rate <= 0 or outstanding <= 0:
            return Decimal('0.00')
        daily_rate = annual_rate / Decimal('100') / Decimal('365')
        return money(money(outstanding) * daily_rate * Decimal(days))

    @staticmethod
    @transaction.atomic
    def defer_scheduled_payment(payment: Payment, *, user=None) -> tuple:
        """Defer one open installment to schedule end; add $35 fee + period interest.

        The fee is always created as a normal scheduled Payment on the original
        date; staff can mark that fee paid later (Interac / manual). Daily
        interest for the extra period is added to the deferred installment and
        loan totals so the open schedule stays aligned with ``total_amount``.
        """
        # Avoid select_related on nullable FKs under FOR UPDATE (Postgres rejects outer joins).
        payment = Payment.objects.select_for_update().select_related('loan').get(
            pk=payment.pk
        )
        loan = Loan.objects.select_for_update().get(pk=payment.loan_id)

        if loan.status not in [
            'pending_signature',
            'pending',
            'pending_funding',
            'active',
            'defaulted',
        ]:
            raise ValueError(f'Cannot defer payments for loan in status: {loan.status}')

        if payment.status not in ('scheduled', 'pending', 'failed', 'nsf'):
            raise ValueError('Only open schedule installments can be deferred.')

        if LoanService.is_deferral_fee_payment(payment):
            raise ValueError('Deferral fee payments cannot be deferred again.')

        if payment.collection_attempts.filter(status__in=['processing', 'completed']).exists():
            raise ValueError(
                'This installment has an active collection and cannot be deferred.'
            )

        previous_date = payment.scheduled_date
        previous_amount = LoanService.money(payment.amount or Decimal('0.00'))
        frequency_days = LoanService._schedule_frequency_days(loan)
        fee_amount = LoanService.money(LoanService.DEFERRAL_FEE_AMOUNT)
        extra_interest = LoanService._deferral_extra_interest(loan, frequency_days)
        total_delta = LoanService.money(fee_amount + extra_interest)

        last_other_date = (
            loan.payments.exclude(pk=payment.pk)
            .order_by('-scheduled_date', '-created_at')
            .values_list('scheduled_date', flat=True)
            .first()
        )
        end_unadjusted = (last_other_date or previous_date) + timedelta(days=frequency_days)
        date_fields = business_calendar.payment_date_fields(end_unadjusted)
        end_date = date_fields['scheduled_date']
        payment.original_date = date_fields['original_date']
        payment.scheduled_date = end_date
        update_fields = ['scheduled_date', 'original_date']
        if extra_interest > 0:
            payment.amount = LoanService.money(previous_amount + extra_interest)
            update_fields.append('amount')
        if payment.status in ('failed', 'nsf'):
            payment.status = 'scheduled'
            payment.failure_reason = None
            payment.processed_at = None
            update_fields.extend(['status', 'failure_reason', 'processed_at'])
        defer_note = f'Deferred from {previous_date}'
        if extra_interest > 0:
            defer_note = f'{defer_note}; daily interest ${extra_interest}'
        existing_notes = (payment.notes or '').strip()
        payment.notes = (
            f'{existing_notes}\n{defer_note}'.strip() if existing_notes else defer_note
        )
        update_fields.append('notes')
        payment.save(update_fields=update_fields)

        loan.fee = LoanService.money((loan.fee or Decimal('0.00')) + total_delta)
        loan.total_amount = LoanService.money(
            (loan.total_amount or Decimal('0.00')) + total_delta
        )
        loan.balance = LoanService.money((loan.balance or Decimal('0.00')) + total_delta)
        loan.save(update_fields=['fee', 'total_amount', 'balance', 'updated_at'])

        fee_payment = Payment.objects.create(
            loan=loan,
            amount=fee_amount,
            type='scheduled',
            status='scheduled',
            scheduled_date=previous_date,
            notes=LoanService.DEFERRAL_FEE_NOTE,
            created_by=user if getattr(user, 'is_authenticated', False) else None,
        )

        from activity.services import actor_label, log_staff_action

        actor = actor_label(user)
        if extra_interest > 0:
            detail = (
                f'Payment deferred by {actor}: installment ${previous_amount} moved from '
                f'{previous_date} to {end_date}; daily interest ${extra_interest} applied '
                f'to installment; $35 deferral fee scheduled as payment on {previous_date}.'
            )
        else:
            detail = (
                f'Payment deferred by {actor}: installment ${previous_amount} moved from '
                f'{previous_date} to {end_date}; $35 deferral fee scheduled as payment on '
                f'{previous_date}.'
            )
        # Load customer only for audit (never fail the defer if audit/customer join breaks).
        try:
            customer = Customer.objects.get(pk=loan.customer_id)
        except Exception:
            customer = None
        if customer is not None:
            log_staff_action(
                customer=customer,
                loan=loan,
                user=user,
                type_value='payment_scheduled',
                title='Payment Deferred',
                description=detail,
                metadata={
                    'action': 'defer_scheduled_payment',
                    'payment_id': str(payment.id),
                    'deferral_fee_payment_id': str(fee_payment.id),
                    'previous_date': str(previous_date),
                    'new_date': str(end_date),
                    'fee_amount': str(fee_amount),
                    'extra_interest': str(extra_interest),
                    'frequency_days': frequency_days,
                    'frequency': LoanService._frequency_key_from_days(frequency_days),
                },
            )
        payment.refresh_from_db()
        fee_payment.refresh_from_db()
        return payment, fee_payment

    @staticmethod
    @transaction.atomic
    def apply_collection_failure_fee(
        collection: CollectionPayment,
        *,
        reason: str = '',
    ) -> Payment | None:
        """Apply the schedule changes for a failed Zūm collection.

        The failed installment stays as history and does not pay down the
        balance. Recovery rows are added before collection-failure fee/interest
        rows. The $50 fee plus extension interest first tops up any
        original remainder installment to the normal installment amount,
        then fills capped extra rows before spilling into a new row.
        """
        # Postgres cannot FOR UPDATE a nullable payment outer join.
        collection = CollectionPayment.objects.select_for_update(of=("self",)).select_related(
            'loan',
            'payment',
        ).get(pk=collection.pk)
        loan = Loan.objects.select_for_update().get(pk=collection.loan_id)
        failed_payment = collection.payment
        fee_amount = LoanService.money(LoanService.COLLECTION_FAILURE_FEE_AMOUNT)
        collection_id = str(collection.id)

        already_applied = loan.payments.filter(
            notes__contains=f'Collection failure id: {collection_id}',
        ).first()
        if already_applied:
            existing_bucket = loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
                notes__contains=f'Collection failure id: {collection_id}',
            ).order_by('scheduled_date', 'created_at', 'id').first()
            if existing_bucket:
                return existing_bucket
            return loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
            ).order_by('scheduled_date', 'created_at', 'id').first()

        frequency_days = LoanService._schedule_frequency_days(loan)
        last_schedule_date = (
            loan.payments.exclude(status='cancelled')
            .order_by('-scheduled_date', '-created_at', '-id')
            .values_list('scheduled_date', flat=True)
            .first()
        )
        base_date = (
            last_schedule_date
            or (
                failed_payment.scheduled_date
                if failed_payment is not None
                else timezone.localdate(collection.initiated_at)
            )
        )
        existing_fee_payment = loan.payments.filter(
            notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
        ).order_by('scheduled_date', 'created_at', 'id').first()

        if existing_fee_payment:
            fee_date = existing_fee_payment.scheduled_date
            missed_date = fee_date - timedelta(days=frequency_days)
        else:
            missed_date = base_date + timedelta(days=frequency_days)
            fee_date = missed_date + timedelta(days=frequency_days)
        missed_amount = LoanService.money(collection.amount or Decimal('0.00'))
        if failed_payment is not None:
            missed_amount = LoanService.money(failed_payment.amount or missed_amount)
        payment_cap = LoanService._collection_failure_payment_cap(
            loan,
            failed_payment,
            missed_amount,
        )
        extension_days = frequency_days * 2
        extra_interest = LoanService._deferral_extra_interest(loan, extension_days)

        recovery_payment = None
        if missed_amount > 0:
            date_fields = business_calendar.payment_date_fields(missed_date)
            recovery_payment = Payment.objects.create(
                loan=loan,
                amount=missed_amount,
                type='scheduled',
                status='scheduled',
                notes=(
                    f'{LoanService.COLLECTION_FAILURE_RECOVERY_NOTE} ${missed_amount}\n'
                    f'Collection failure id: {collection_id}\n'
                    f'Reason: {(reason or collection.failure_reason or "Unknown").strip()}'
                ),
                **date_fields,
            )
        failure_note = (
            f'Collection failure id: {collection_id}\n'
            f'Reason: {(reason or collection.failure_reason or "Unknown").strip()}'
        )
        extra_amount = LoanService.money(fee_amount + extra_interest)
        fee_payment = LoanService._allocate_collection_failure_extra(
            loan,
            amount=extra_amount,
            cap=payment_cap,
            first_bucket_date=fee_date,
            frequency_days=frequency_days,
            failure_note=(
                f'{failure_note}\n'
                f'NSF fee: ${fee_amount}\n'
                f'Extension interest: ${extra_interest}'
            ),
        )

        total_delta = extra_amount
        loan.fee = LoanService.money((loan.fee or Decimal('0.00')) + total_delta)
        loan.total_amount = LoanService.money(
            (loan.total_amount or Decimal('0.00')) + total_delta
        )
        loan.balance = LoanService.money((loan.balance or Decimal('0.00')) + total_delta)
        loan.save(update_fields=['fee', 'total_amount', 'balance', 'updated_at'])

        from .collection_policy import should_enter_collections, should_stop_collections

        failure_reason = reason or collection.failure_reason
        if loan.status == 'stopped' or should_stop_collections(loan, failure_reason):
            loan.mark_stopped(notes=f'Collections stopped: {failure_reason or "missed collections"}')
        elif loan.status == 'active' and should_enter_collections(loan, failure_reason):
            loan.mark_defaulted(notes=f'In collections: {failure_reason or "missed payment"}')

        return fee_payment
    @staticmethod
    @transaction.atomic
    def mark_deferral_fee_paid(
        payment: Payment,
        *,
        method: str = 'etransfer',
        reference: str = '',
        user=None,
    ) -> Payment:
        """Mark a $35 deferral-fee Payment as paid (Interac or manual)."""
        if method not in ('etransfer', 'manual'):
            raise ValueError('Fee payment method must be etransfer or manual.')

        payment = Payment.objects.select_for_update().select_related('loan').get(pk=payment.pk)
        loan = payment.loan

        if not LoanService.is_deferral_fee_payment(payment):
            raise ValueError('Only the $35 deferral fee payment can be marked paid here.')

        if payment.status == 'completed':
            raise ValueError('This deferral fee is already marked paid.')

        if payment.status not in ('scheduled', 'pending', 'failed', 'nsf'):
            raise ValueError('This deferral fee cannot be marked paid in its current status.')

        if payment.collection_attempts.filter(status__in=['processing', 'completed']).exists():
            raise ValueError(
                'This deferral fee has an active collection and cannot be marked paid manually.'
            )

        payment.type = method
        payment.status = 'completed'
        payment.processed_at = timezone.now()
        payment.failure_reason = None
        payment.reference = (reference or '').strip() or (
            'INTERAC-DEFERRAL-FEE' if method == 'etransfer' else 'MANUAL-DEFERRAL-FEE'
        )
        payment.save(update_fields=[
            'type',
            'status',
            'processed_at',
            'failure_reason',
            'reference',
        ])
        loan.apply_payment(payment.amount, user=user)

        from activity.services import actor_label, log_staff_action

        actor = actor_label(user)
        method_label = 'Interac' if method == 'etransfer' else 'manual'
        try:
            customer = Customer.objects.get(pk=loan.customer_id)
        except Exception:
            customer = None
        if customer is not None:
            log_staff_action(
                customer=customer,
                loan=loan,
                user=user,
                type_value='payment_completed',
                title='Deferral Fee Marked Paid',
                description=(
                    f'$35 deferral fee marked paid ({method_label}) by {actor} '
                    f'for {payment.scheduled_date}.'
                ),
                metadata={
                    'action': 'mark_deferral_fee_paid',
                    'payment_id': str(payment.id),
                    'method': method,
                    'amount': str(payment.amount),
                    'reference': payment.reference,
                },
            )
        payment.refresh_from_db()
        return payment
