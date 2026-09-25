from io import BytesIO
from decimal import Decimal

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .services import LoanService


def _money(value):
    return f"${LoanService.money(value or Decimal('0.00')):,.2f}"


def _date(value):
    if not value:
        return "-"
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)


def _table(data, widths):
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1d5db")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (-1, 1), (-1, -1), "RIGHT"),
            ]
        )
    )
    return table


def build_trustee_statement_pdf(loan, fees):
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.55 * inch,
        leftMargin=0.55 * inch,
        topMargin=0.55 * inch,
        bottomMargin=0.55 * inch,
        title="Statement",
        pageCompression=0,
    )
    styles = getSampleStyleSheet()
    story = []
    customer = loan.customer
    lender = getattr(customer, "lender", None) or getattr(loan, "lender", None)
    lender_name = getattr(lender, "name", None) or "MohawkLoans"
    generated_at = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M")

    story.append(Paragraph(f"{lender_name} Statement", styles["Title"]))
    story.append(Paragraph(f"Generated: {generated_at}", styles["Normal"]))
    story.append(Spacer(1, 12))

    summary = [
        ["Customer", f"{customer.first_name} {customer.last_name}".strip()],
        ["Email", customer.email or "-"],
        ["Phone", customer.phone or "-"],
        ["Loan ID", str(loan.id)],
        ["Loan status", loan.get_status_display()],
        ["Principal", _money(loan.principal)],
        ["Total amount", _money(loan.total_amount)],
        ["Current balance", _money(loan.balance)],
        ["Funded date", _date(loan.funded_at)],
    ]
    story.append(_table(summary, [1.6 * inch, 4.8 * inch]))
    story.append(Spacer(1, 14))

    fee_total = sum((fee["amount"] for fee in fees), Decimal("0.00"))
    story.append(Paragraph("Added Fees", styles["Heading2"]))
    if fees:
        fee_rows = [["Date", "Fee name", "Amount"]]
        fee_rows.extend(
            [[_date(fee["date"]), fee["name"], _money(fee["amount"])] for fee in fees]
        )
        fee_rows.append(["", "Added fee total", _money(fee_total)])
        story.append(_table(fee_rows, [1.2 * inch, 4.0 * inch, 1.2 * inch]))
    else:
        story.append(Paragraph("No additional statement fees were added.", styles["Normal"]))
    story.append(Spacer(1, 14))

    story.append(Paragraph("Payment Schedule", styles["Heading2"]))
    payments = list(
        loan.payments.exclude(status="cancelled").order_by("scheduled_date", "created_at", "id")
    )
    if payments:
        payment_rows = [["Date", "Type", "Status", "Notes", "Amount"]]
        payment_rows.extend(
            [
                [
                    _date(payment.scheduled_date),
                    payment.get_type_display(),
                    payment.get_status_display(),
                    payment.notes or "",
                    _money(payment.amount),
                ]
                for payment in payments
            ]
        )
        story.append(
            _table(
                payment_rows,
                [0.9 * inch, 1.0 * inch, 1.0 * inch, 2.6 * inch, 0.9 * inch],
            )
        )
    else:
        story.append(Paragraph("No payment schedule rows are recorded.", styles["Normal"]))
    story.append(Spacer(1, 14))

    statement_total = LoanService.money((loan.balance or Decimal("0.00")) + fee_total)
    totals = [
        ["Current balance", _money(loan.balance)],
        ["Added statement fees", _money(fee_total)],
        ["Statement total", _money(statement_total)],
    ]
    story.append(_table(totals, [4.9 * inch, 1.5 * inch]))
    story.append(Spacer(1, 10))
    story.append(
        Paragraph(
            "This statement is generated for review. Added fees shown here are included "
            "for this statement download and do not change the loan ledger unless recorded separately.",
            styles["Italic"],
        )
    )

    doc.build(story)
    buffer.seek(0)
    return buffer
