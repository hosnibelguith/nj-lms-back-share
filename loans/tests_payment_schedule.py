from datetime import date, datetime
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
