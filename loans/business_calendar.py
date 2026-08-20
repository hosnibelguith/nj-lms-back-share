"""
Payment-schedule business calendar.

A business day is Monday–Friday excluding bank holidays stored in BankHoliday.
Dates always move backward to the previous business day. Collection instructions
are eligible after 7:00 PM America/Toronto on the calendar day before the
adjusted payment date.
"""
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

MANAGED_HOLIDAY_YEARS = tuple(range(2026, 2032))  # 2026–2031 inclusive
MIN_UPLOAD_YEARS = 2
INSTRUCTION_SEND_TIME = time(19, 1)  # 7:01 PM — after 7:00 PM
BUSINESS_TIMEZONE_NAME = "America/Toronto"


def business_timezone():
    return ZoneInfo(getattr(settings, "TIME_ZONE", None) or BUSINESS_TIMEZONE_NAME)


def local_now(at=None):
    dt = at or timezone.now()
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, business_timezone())
    return timezone.localtime(dt, business_timezone())


def local_today(at=None):
    return local_now(at).date()


def holiday_writes_blocked(at=None):
    """New Year's Day is itself a holiday — do not upload the calendar on Jan 1."""
    today = local_today(at)
    return today.month == 1 and today.day == 1


def holiday_write_block_message():
    return (
        "Bank holiday calendars cannot be uploaded on January 1 because it is "
        "itself a holiday. Enter holidays before January 1."
    )


def add_calendar_months(start: date, months: int) -> date:
    """Advance by calendar months using min(selected day, days in that month)."""
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, monthrange(year, month)[1])
    return date(year, month, day)


def normalized_month_day(year: int, month: int, selected_day: int) -> date:
    """actual day = min(selected day, number of days in that month)."""
    day = min(int(selected_day), monthrange(year, month)[1])
    return date(year, month, day)


def iter_twice_monthly_unadjusted_dates(start_date: date, count: int, day_a: int, day_b: int):
    """Yield unique twice-monthly dates on or after start_date.

    Each selected day is normalized independently. If both land on the same
    calendar date in a short month, only one payment is created that month.
    """
    selected_days = sorted({int(day_a), int(day_b)})
    year = start_date.year
    month = start_date.month
    produced = 0
    last_date = None
    guard = 0
    while produced < int(count) and guard < 720:
        guard += 1
        month_dates = []
        for selected in selected_days:
            value = normalized_month_day(year, month, selected)
            if not month_dates or month_dates[-1] != value:
                month_dates.append(value)
        for value in month_dates:
            if value < start_date:
                continue
            if last_date is not None and value <= last_date:
                continue
            yield value
            last_date = value
            produced += 1
            if produced >= int(count):
                return
        month += 1
        if month > 12:
            month = 1
            year += 1


def unadjusted_date_at(start_date: date, index: int, frequency_days: int) -> date:
    if int(frequency_days) >= 28:
        return add_calendar_months(start_date, index)
    return start_date + timedelta(days=int(frequency_days) * index)


def iter_unadjusted_dates(
    start_date: date,
    count: int,
    frequency_days: int,
    month_days=None,
):
    if month_days:
        yield from iter_twice_monthly_unadjusted_dates(
            start_date,
            count,
            month_days[0],
            month_days[1],
        )
        return
    for index in range(count):
        yield unadjusted_date_at(start_date, index, frequency_days)


def holiday_dates_for_years(years) -> set:
    from .models import BankHoliday

    year_set = {int(year) for year in years if year is not None}
    if not year_set:
        return set()
    lookup_years = set(year_set)
    for year in year_set:
        lookup_years.add(year - 1)
    return set(
        BankHoliday.objects.filter(date__year__in=lookup_years).values_list(
            "date", flat=True
        )
    )


def years_missing_holidays(dates) -> list:
    """Years that appear in `dates` and have no holiday rows entered."""
    from .models import BankHoliday

    years = sorted({d.year for d in dates if d is not None})
    if not years:
        return []
    present = set(
        BankHoliday.objects.filter(date__year__in=years)
        .values_list("date__year", flat=True)
        .distinct()
    )
    return [year for year in years if year not in present]


def is_weekend(value: date) -> bool:
    return value.weekday() >= 5


def is_business_day(value: date, holidays=None) -> bool:
    if is_weekend(value):
        return False
    if holidays is None:
        holidays = holiday_dates_for_years({value.year})
    return value not in holidays


def previous_business_day(value: date, holidays=None) -> date:
    """Move backward one calendar day at a time until the date is a business day."""
    if holidays is None:
        holidays = holiday_dates_for_years({value.year, value.year - 1})
    current = value
    for _ in range(31):
        if is_business_day(current, holidays):
            return current
        current = current - timedelta(days=1)
    return current


def adjust_payment_date(unadjusted: date, holidays=None):
    """Return (original_date, adjusted_payment_date). Never moves forward."""
    return unadjusted, previous_business_day(unadjusted, holidays)


def payment_date_fields(unadjusted: date, holidays=None) -> dict:
    original, adjusted = adjust_payment_date(unadjusted, holidays)
    return {"original_date": original, "scheduled_date": adjusted}


def instruction_send_at(adjusted_date: date):
    """7:01 PM business timezone on the calendar day before the adjusted date."""
    send_date = adjusted_date - timedelta(days=1)
    naive = datetime.combine(send_date, INSTRUCTION_SEND_TIME)
    tz = business_timezone()
    if timezone.is_naive(naive):
        return timezone.make_aware(naive, tz)
    return naive.astimezone(tz)


def is_instruction_send_ready(adjusted_date: date, at=None) -> bool:
    return local_now(at) >= instruction_send_at(adjusted_date)


def missing_holiday_warning(years) -> str | None:
    if not years:
        return None
    labels = ", ".join(str(year) for year in years)
    return (
        f"Bank holidays have not been entered for {labels}. "
        "Weekend adjustment still applies; holiday dates will not shift until "
        "that year's calendar is uploaded."
    )
