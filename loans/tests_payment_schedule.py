from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Customer, User
from banking.models import BankAccount, BankConnection
from loans.business_calendar import (
    INSTRUCTION_SEND_TIME,
    MANAGED_HOLIDAY_YEARS,
    add_calendar_months,
    instruction_send_at,
    is_instruction_send_ready,
    previous_business_day,
)
from loans.models import BankHoliday, Loan, LoanFormula, Payment
from loans.services import LoanService
from loans.tasks import process_scheduled_payments


TORONTO = ZoneInfo("America/Toronto")


def toronto_dt(year, month, day, hour, minute=0):
    return timezone.make_aware(datetime(year, month, day, hour, minute), TORONTO)


@override_settings(TIME_ZONE="America/Toronto", USE_TZ=True)
class PaymentScheduleDateLogicTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="holiday-agent@example.com",
            password="password123",
            full_name="Holiday Agent",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.customer = Customer.objects.create(
            first_name="Holiday",
            last_name="Customer",
            email="holiday-customer@example.com",
            phone="4165550100",
            province="ON",
            status="active",
        )
        self.formula = LoanFormula.objects.create(
            name="Holiday Formula 500",
            principal_amount=Decimal("500.00"),
            brokerage_percent=Decimal("70.00"),
            repayment_percent=Decimal("35.00"),
            default_number_of_payments=4,
            default_frequency_days=14,
            is_active=True,
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("500.00"),
            fee=Decimal("350.00"),
            total_amount=Decimal("850.00"),
            balance=Decimal("850.00"),
            status="active",
            formula=self.formula,
            is_active=True,
        )
        self.client.force_authenticate(user=self.staff)

    def test_saturday_and_sunday_move_to_friday(self):
        saturday = date(2026, 5, 16)
        sunday = date(2026, 5, 17)
        self.assertEqual(previous_business_day(saturday), date(2026, 5, 15))
        self.assertEqual(previous_business_day(sunday), date(2026, 5, 15))

    def test_monday_holiday_moves_to_previous_friday(self):
        BankHoliday.objects.create(date=date(2026, 6, 15), name="Test Holiday")
        self.assertEqual(previous_business_day(date(2026, 6, 15)), date(2026, 6, 12))

    def test_never_moves_payment_forward(self):
        friday = date(2026, 5, 15)
        self.assertEqual(previous_business_day(friday), friday)
        BankHoliday.objects.create(date=friday, name="Friday holiday")
        self.assertEqual(previous_business_day(friday), date(2026, 5, 14))
        self.assertLess(previous_business_day(friday), friday)

    def test_monthly_short_month_uses_last_day_then_adjusts(self):
        self.assertEqual(add_calendar_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        payments = LoanService.generate_payment_schedule(
            self.loan,
            num_payments=3,
            payment_amount=Decimal("200.00"),
            start_date=date(2026, 1, 31),
            frequency_days=30,
            schedule_total=Decimal("600.00"),
        )
        self.assertEqual(payments[0].original_date, date(2026, 1, 31))
        self.assertEqual(payments[0].scheduled_date, date(2026, 1, 30))  # Sat -> Fri
        self.assertEqual(payments[1].original_date, date(2026, 2, 28))
        self.assertEqual(payments[1].scheduled_date, date(2026, 2, 27))  # Sat -> Fri
        self.assertEqual(payments[2].original_date, date(2026, 3, 31))
        self.assertEqual(payments[2].scheduled_date, date(2026, 3, 31))  # Tuesday

    def test_weekly_cadence_stays_on_unadjusted_dates(self):
        payments = LoanService.generate_payment_schedule(
            self.loan,
            num_payments=2,
            payment_amount=Decimal("100.00"),
            start_date=date(2026, 5, 16),  # Saturday
            frequency_days=7,
            schedule_total=Decimal("200.00"),
        )
        self.assertEqual(payments[0].original_date, date(2026, 5, 16))
        self.assertEqual(payments[0].scheduled_date, date(2026, 5, 15))
        self.assertEqual(payments[1].original_date, date(2026, 5, 23))
        self.assertEqual(payments[1].scheduled_date, date(2026, 5, 22))

    def test_twice_monthly_uses_two_selected_days_independently(self):
        payments = LoanService.generate_payment_schedule(
            self.loan,
            num_payments=4,
            payment_amount=Decimal("150.00"),
            start_date=date(2026, 8, 1),
            frequency_days=15,
            schedule_total=Decimal("600.00"),
            month_days=[15, 31],
        )
        self.assertEqual(
            [payment.original_date for payment in payments],
            [
                date(2026, 8, 15),
                date(2026, 8, 31),
                date(2026, 9, 15),
                date(2026, 9, 30),
            ],
        )
        self.assertEqual(payments[1].scheduled_date, date(2026, 8, 31))
        self.assertEqual(payments[3].original_date, date(2026, 9, 30))

    def test_twice_monthly_short_month_does_not_skip_february(self):
        payments = LoanService.generate_payment_schedule(
            self.loan,
            num_payments=4,
            payment_amount=Decimal("150.00"),
            start_date=date(2026, 1, 31),
            frequency_days=15,
            schedule_total=Decimal("600.00"),
            month_days=[15, 31],
        )
        originals = [payment.original_date for payment in payments]
        self.assertEqual(originals[0], date(2026, 1, 31))
        self.assertEqual(originals[1], date(2026, 2, 15))
        self.assertEqual(originals[2], date(2026, 2, 28))
        self.assertEqual(originals[3], date(2026, 3, 15))

    def test_twice_monthly_does_not_create_duplicate_when_days_collapse(self):
        from loans.business_calendar import iter_twice_monthly_unadjusted_dates

        dates = list(
            iter_twice_monthly_unadjusted_dates(date(2026, 2, 1), 3, 30, 31)
        )
        self.assertEqual(dates[0], date(2026, 2, 28))
        self.assertEqual(dates.count(date(2026, 2, 28)), 1)
        self.assertEqual(dates[1], date(2026, 3, 30))
        self.assertEqual(dates[2], date(2026, 3, 31))

    def test_adjust_schedule_twice_monthly_requires_two_days(self):
        start_date = date(2026, 8, 7)
        rejected = self.client.patch(
            f"/api/loans/{self.loan.id}/adjust-schedule/",
            {
                "payment_amount": "180.00",
                "frequency": "twice-monthly",
                "start_date": start_date.isoformat(),
            },
            format="json",
        )
        self.assertEqual(rejected.status_code, 400)

        accepted = self.client.patch(
            f"/api/loans/{self.loan.id}/adjust-schedule/",
            {
                "payment_amount": "180.00",
                "frequency": "twice-monthly",
                "start_date": start_date.isoformat(),
                "month_days": [15, 31],
            },
            format="json",
        )
        self.assertEqual(accepted.status_code, 200, accepted.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.schedule_frequency, "twice-monthly")
        self.assertEqual(self.loan.twice_monthly_day_1, 15)
        self.assertEqual(self.loan.twice_monthly_day_2, 31)
        customer = self.client.get(f"/api/customers/{self.customer.id}/loans/")
        self.assertEqual(customer.status_code, 200, customer.data)
        self.assertEqual(customer.data[0]["frequency"], "twice-monthly")
        self.assertEqual(customer.data[0]["twice_monthly_days"], [15, 31])

    def test_instruction_send_is_day_before_after_7pm(self):
        send_at = instruction_send_at(date(2026, 6, 12))
        self.assertEqual(send_at.date(), date(2026, 6, 11))
        self.assertEqual(send_at.timetz().replace(tzinfo=None), INSTRUCTION_SEND_TIME)
        self.assertGreater(INSTRUCTION_SEND_TIME.hour, 18)
        self.assertNotEqual(INSTRUCTION_SEND_TIME.minute, 0)

        before = toronto_dt(2026, 6, 11, 19, 0)
        after = toronto_dt(2026, 6, 11, 19, 1)
        self.assertFalse(is_instruction_send_ready(date(2026, 6, 12), at=before))
        self.assertTrue(is_instruction_send_ready(date(2026, 6, 12), at=after))

    def test_missing_holiday_year_warns_on_loan(self):
        LoanService.generate_payment_schedule(
            self.loan,
            num_payments=1,
            payment_amount=Decimal("100.00"),
            start_date=date(2026, 5, 15),
            frequency_days=14,
            schedule_total=Decimal("100.00"),
        )
        response = self.client.get(f"/api/loans/{self.loan.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["holiday_warnings"])
        self.assertIn("2026", response.data["holiday_warnings"][0])

        BankHoliday.objects.create(date=date(2026, 7, 1), name="Canada Day")
        response = self.client.get(f"/api/loans/{self.loan.id}/")
        self.assertEqual(response.data["holiday_warnings"], [])


@override_settings(TIME_ZONE="America/Toronto", USE_TZ=True, ZUMRAILS_DRY_RUN=True)
class ScheduledCollectionSendWindowTests(APITestCase):
    def setUp(self):
        self.customer = Customer.objects.create(
            first_name="Send",
            last_name="Window",
            email="send-window@example.com",
            phone="4165550102",
            province="ON",
            status="active",
        )
        connection = BankConnection.objects.create(
            customer=self.customer,
            login_id="login-send",
            sync_status="synced",
        )
        account = BankAccount.objects.create(
            connection=connection,
            customer=self.customer,
            external_id="acct-send",
            name="Chequing",
            type="checking",
            transit_number="12345",
            institution_number="003",
            account_number="1234567890",
            is_primary=True,
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("500.00"),
            fee=Decimal("100.00"),
            total_amount=Decimal("600.00"),
            balance=Decimal("600.00"),
            status="active",
            bank_account=account,
            collections_account=account,
            is_active=True,
        )

    @patch("loans.business_calendar.timezone.now")
    def test_does_not_send_tomorrow_before_cutoff(self, mock_now):
        mock_now.return_value = toronto_dt(2026, 6, 11, 18, 59)
        payment = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=date(2026, 6, 12),
            status="scheduled",
        )
        result = process_scheduled_payments()
        payment.refresh_from_db()
        self.assertEqual(result["initiated"], 0)
        self.assertEqual(payment.status, "scheduled")

    @patch("loans.business_calendar.timezone.now")
    def test_sends_tomorrow_after_7pm(self, mock_now):
        mock_now.return_value = toronto_dt(2026, 6, 11, 19, 1)
        payment = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=date(2026, 6, 12),
            status="scheduled",
        )
        result = process_scheduled_payments()
        payment.refresh_from_db()
        self.assertEqual(result["initiated"], 1)
        self.assertEqual(payment.status, "pending")


@override_settings(TIME_ZONE="America/Toronto", USE_TZ=True)
class BankHolidayCalendarApiTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="calendar-agent@example.com",
            password="password123",
            full_name="Calendar Agent",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.client.force_authenticate(user=self.staff)

    def test_calendar_lists_managed_years_2026_to_2031(self):
        response = self.client.get("/api/bank-holidays/calendar/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["managed_years"], list(MANAGED_HOLIDAY_YEARS))
        self.assertEqual(response.data["min_upload_years"], 2)
        self.assertEqual(
            [row["year"] for row in response.data["years"]],
            list(MANAGED_HOLIDAY_YEARS),
        )
        self.assertEqual(response.data["instruction_send_time"], "19:01")

    def test_manual_add_is_year_scoped(self):
        response = self.client.post(
            "/api/bank-holidays/",
            {"date": "2026-07-01", "name": "Canada Day"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["year"], 2026)

    def test_upload_requires_at_least_two_years(self):
        response = self.client.post(
            "/api/bank-holidays/upload/",
            {
                "holidays": [
                    {"date": "2026-07-01", "name": "Canada Day"},
                    {"date": "2026-12-25", "name": "Christmas Day"},
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("at least 2 years", response.data["error"])

        ok = self.client.post(
            "/api/bank-holidays/upload/",
            {
                "holidays": [
                    {"date": "2026-07-01", "name": "Canada Day"},
                    {"date": "2027-12-25", "name": "Christmas Day"},
                ]
            },
            format="json",
        )
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertEqual(ok.data["years"], [2026, 2027])
        self.assertEqual(BankHoliday.objects.count(), 2)

    @patch("loans.business_calendar.timezone.now")
    def test_upload_blocked_on_january_first(self, mock_now):
        mock_now.return_value = toronto_dt(2027, 1, 1, 10, 0)
        response = self.client.post(
            "/api/bank-holidays/upload/",
            {
                "holidays": [
                    {"date": "2026-07-01", "name": "Canada Day"},
                    {"date": "2027-12-25", "name": "Christmas Day"},
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("January 1", response.data["error"])
        create = self.client.post(
            "/api/bank-holidays/",
            {"date": "2026-07-01", "name": "Canada Day"},
            format="json",
        )
        self.assertEqual(create.status_code, 400)

    def test_reject_holiday_outside_managed_years(self):
        response = self.client.post(
            "/api/bank-holidays/",
            {"date": "2025-12-25", "name": "Christmas Day"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class PaymentDateActivityHistoryTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="activity-agent@example.com",
            password="password123",
            full_name="Activity Agent",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.customer = Customer.objects.create(
            first_name="Activity",
            last_name="Customer",
            email="activity-customer@example.com",
            phone="4165550101",
            province="ON",
            status="active",
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("500.00"),
            fee=Decimal("100.00"),
            total_amount=Decimal("600.00"),
            balance=Decimal("600.00"),
            status="active",
            is_active=True,
        )
        self.client.force_authenticate(user=self.staff)

    def test_generate_schedule_logs_weekend_date_adjustments(self):
        from activity.models import ActivityHistory

        LoanService.generate_payment_schedule(
            self.loan,
            num_payments=2,
            payment_amount=Decimal("300.00"),
            start_date=date(2026, 5, 16),
            frequency_days=7,
            schedule_total=Decimal("600.00"),
        )

        logs = list(
            ActivityHistory.objects.filter(
                loan=self.loan,
                title="Date Adjusted",
            ).order_by("created_at")
        )
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0].created_by, "system")
        self.assertIn("2026-05-16", logs[0].description)
        self.assertIn("2026-05-15", logs[0].description)
        self.assertIn("weekend", logs[0].description)
        self.assertIn("previous business day", logs[0].description)

    def test_weekday_payment_create_does_not_log_date_adjusted(self):
        from activity.models import ActivityHistory

        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=date(2026, 5, 15),
            original_date=date(2026, 5, 15),
            status="scheduled",
        )

        self.assertFalse(
            ActivityHistory.objects.filter(
                loan=self.loan,
                title="Date Adjusted",
            ).exists()
        )

    def test_staff_date_edit_logs_once_and_notes_business_day_shift(self):
        from activity.models import ActivityHistory

        today = timezone.localdate()
        saturday = today + timedelta(days=1)
        while saturday.weekday() != 5:
            saturday += timedelta(days=1)
        weekday = previous_business_day(saturday)

        payment = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=weekday,
            original_date=weekday,
            status="scheduled",
        )

        LoanService.update_scheduled_payment(
            payment,
            scheduled_date=saturday,
            user=self.staff,
        )

        staff_logs = ActivityHistory.objects.filter(
            loan=self.loan,
            title="Payment Installment Updated",
        )
        system_logs = ActivityHistory.objects.filter(
            loan=self.loan,
            title="Date Adjusted",
        )
        self.assertEqual(staff_logs.count(), 1)
        self.assertEqual(system_logs.count(), 0)
        self.assertIn("previous business day", staff_logs.get().description)
        self.assertIn(str(saturday), staff_logs.get().description)
        self.assertIn(str(weekday), staff_logs.get().description)

    def test_direct_scheduled_date_change_logs_system_adjustment(self):
        from activity.models import ActivityHistory

        payment = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("80.00"),
            scheduled_date=date(2026, 5, 15),
            original_date=date(2026, 5, 15),
            status="scheduled",
        )
        payment.scheduled_date = date(2026, 5, 14)
        payment.original_date = date(2026, 5, 16)
        payment.save(update_fields=["scheduled_date", "original_date"])

        log = ActivityHistory.objects.get(loan=self.loan, title="Date Adjusted")
        self.assertEqual(log.created_by, "system")
        self.assertEqual(log.metadata.get("actor"), "System")
        self.assertIn("2026-05-15", log.description)
        self.assertIn("2026-05-14", log.description)
