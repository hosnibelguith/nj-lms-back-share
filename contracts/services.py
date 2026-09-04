"""
Render the Mohawk loan agreement with dynamic variables.
Landing and Arrive share one template; funding language follows the channel.
Customer address fields are omitted until IBV provides them.
"""
from __future__ import annotations

from decimal import Decimal
from html import escape
from typing import Any

from django.utils import timezone

from accounts.models import Customer, GlobalSetting


AGREEMENT_VERSION = "mohawk-channel-v2"


def is_arrive_customer(customer) -> bool:
    """Same rule as loan/staff flows: Arrive source or Arrive application id."""
    return bool(
        getattr(customer, "source", None) == Customer.SOURCE_ARRIVE
        or getattr(customer, "arrive_application_id", None)
    )


def _channel_copy(customer) -> dict[str, str]:
    """Landing vs Arrive funding language. Shared legal body stays one template."""
    if is_arrive_customer(customer):
        return {
            "application_channel": "Arrive",
            "funding_authorization_heading": (
                "Amount Financed and Secured Card Funding Authorization"
            ),
            "funding_disbursement_terms": (
                "THE BORROWER WILL NOT RECEIVE THE PRINCIPAL AMOUNT AS A CASH "
                "DEPOSIT OR DEPOSIT INTO THE BORROWER'S BANK ACCOUNT. Instead, "
                "upon approval and completion of all required documentation, the "
                "applicable funds will be paid or applied directly to fund the "
                "Borrower's secured card account (the \"Secured Card Funding\"). "
                "The date on which the funds are applied to the secured card "
                "account is referred to as the \"Funding Date.\" The Borrower "
                "expressly authorizes the Lender and its service providers to "
                "direct the applicable financed amount to the secured card "
                "program for this purpose."
            ),
            "funding_repayment_ack": (
                "The Borrower acknowledges that this financing creates a "
                "repayment obligation even though the financed funds are not "
                "deposited into the Borrower's bank account. The Borrower's bank "
                "account information is collected and used for identity and "
                "account verification and for repayment by Pre-Authorized Debit, "
                "as described in this Agreement and the annexed PAD Agreement."
            ),
            "funding_principal_components": "secured card funding amount",
            "funding_example_disbursement_label": (
                "Amount applied to fund the secured card"
            ),
            "funding_example_disbursement_total_label": "Secured card funding",
            "rescission_return_terms": (
                "the full Secured Card Funding is returned, reversed, or "
                "otherwise made available to Us in accordance with instructions "
                "provided by Us. Exercising this right may require cancellation "
                "or reversal of the corresponding secured card funding transaction."
            ),
            "funding_amount_label": "secured card funding amount",
            "principal_funding_component": (
                "the amount applied to fund the Borrower's secured card account"
            ),
            "principal_deposit_disclaimer": (
                "No portion of the Principal Amount is required to be deposited "
                "into the Borrower's bank account."
            ),
        }
    return {
        "application_channel": "Landing",
        "funding_authorization_heading": (
            "Amount Financed and Bank Account Funding Authorization"
        ),
        "funding_disbursement_terms": (
            "Upon approval and completion of all required documentation, the "
            "applicable funds will be paid or deposited to the Borrower's "
            "designated bank account by electronic funds transfer (EFT) or "
            "Interac e-Transfer (the \"Bank Account Funding\"). The date on "
            "which the funds are deposited or sent is referred to as the "
            "\"Funding Date.\" The Borrower expressly authorizes the Lender and "
            "its service providers to direct the applicable financed amount to "
            "the Borrower's designated bank account for this purpose."
        ),
        "funding_repayment_ack": (
            "The Borrower acknowledges that this financing creates a repayment "
            "obligation. The Borrower's bank account information is collected "
            "and used for identity and account verification, for disbursement of "
            "the Principal Amount, and for repayment by Pre-Authorized Debit, as "
            "described in this Agreement and the annexed PAD Agreement."
        ),
        "funding_principal_components": "bank account funding amount",
        "funding_example_disbursement_label": (
            "Amount deposited to the borrower's bank account"
        ),
        "funding_example_disbursement_total_label": "Bank account funding",
        "rescission_return_terms": (
            "the full Bank Account Funding is returned, reversed, or otherwise "
            "made available to Us in accordance with instructions provided by "
            "Us. Exercising this right may require reversal of the corresponding "
            "bank account funding transaction."
        ),
        "funding_amount_label": "bank account funding amount",
        "principal_funding_component": (
            "the amount disbursed to the Borrower's bank account"
        ),
        "principal_deposit_disclaimer": (
            "The Principal Amount, less any financed cosigner or third-party "
            "service fee, is disbursed to the Borrower's designated bank account."
        ),
    }


def _money(value) -> str:
    try:
        amount = Decimal(str(value if value is not None else "0"))
    except Exception:
        amount = Decimal("0")
    return f"{amount.quantize(Decimal('0.01')):,.2f}"


def _plain(value: Any, fallback: str = "—") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def _esc(value: Any, fallback: str = "—") -> str:
    return escape(_plain(value, fallback))


def _lender_settings() -> dict[str, str]:
    return {
        "lending_license_holder": GlobalSetting.get_value(
            "LENDING_LICENSE_HOLDER", "MohawkLoans"
        )
        or "MohawkLoans",
        "lending_license_holder_address": GlobalSetting.get_value(
            "LENDING_LICENSE_HOLDER_ADDRESS",
            "Mohawk Territory of Kanehsatake, Quebec, Canada",
        )
        or "Mohawk Territory of Kanehsatake, Quebec, Canada",
        "lending_license_holder_email": GlobalSetting.get_value(
            "LENDING_LICENSE_HOLDER_EMAIL", "support@mohawkloans.com"
        )
        or "support@mohawkloans.com",
        "lending_license_holder_phone": GlobalSetting.get_value(
            "LENDING_LICENSE_HOLDER_PHONE", ""
        )
        or "—",
        "lending_license_holder_website": GlobalSetting.get_value(
            "LENDING_LICENSE_HOLDER_WEBSITE", "https://mohawkloans.com"
        )
        or "https://mohawkloans.com",
    }


def _primary_bank_account(customer):
    account = (
        customer.bank_accounts.filter(use_for_eft_collections=True).order_by("-updated_at").first()
        or customer.bank_accounts.filter(is_primary=True).order_by("-updated_at").first()
        or customer.bank_accounts.order_by("-is_primary", "-updated_at").first()
    )
    return account


def _payment_frequency_label(frequency_days: int) -> str:
    if frequency_days == 14:
        return "every 14 days (biweekly)"
    if frequency_days == 7:
        return "every 7 days (weekly)"
    if frequency_days in (30, 31):
        return "monthly"
    return f"every {frequency_days} days"


def _build_amortization_table(loan) -> str:
    payments = list(loan.payments.order_by("scheduled_date", "created_at"))
    if not payments:
        return (
            '<p class="muted">Payment schedule will be confirmed from your loan terms.</p>'
        )

    rows = []
    for index, payment in enumerate(payments, start=1):
        rows.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td>{_esc(payment.scheduled_date)}</td>"
            f"<td class='num'>${_esc(_money(payment.amount))}</td>"
            f"<td>{_esc(payment.get_status_display() if hasattr(payment, 'get_status_display') else payment.status)}</td>"
            "</tr>"
        )

    return (
        '<div class="table-wrap"><table class="schedule">'
        "<thead><tr>"
        "<th>#</th><th>Due date</th><th>Amount</th><th>Status</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table></div>"
    )


def build_agreement_context(customer, loan) -> dict[str, str]:
    lender = _lender_settings()
    bank = _primary_bank_account(customer)
    formula = getattr(loan, "formula", None)
    frequency_days = int(getattr(formula, "default_frequency_days", None) or 14)
    payments = list(loan.payments.order_by("scheduled_date", "created_at"))
    first_payment = payments[0] if payments else None
    first_amount = (
        first_payment.amount
        if first_payment
        else (loan.total_amount or loan.principal or Decimal("0"))
    )
    first_date = first_payment.scheduled_date if first_payment else None
    num_payments = (
        len(payments)
        or int(getattr(formula, "default_number_of_payments", None) or 0)
        or 1
    )
    today = timezone.localdate().isoformat()
    phone = customer.phone or ""

    return {
        **lender,
        **_channel_copy(customer),
        "customer_name": customer.full_name or f"{customer.first_name} {customer.last_name}".strip(),
        "customer_phone_number": phone,
        "customer_mobile_phone": phone,
        "date_today": today,
        "amount_financed": _money(loan.principal),
        "loan_number_of_payments": str(num_payments),
        "loan_first_payment_amount": _money(first_amount),
        "loan_first_payment_date": _plain(first_date),
        "loan_pending_first_payment_date": _plain(first_date),
        "pay_frequency": _payment_frequency_label(frequency_days),
        "amortization_schedule_table_small": _build_amortization_table(loan),
        "bank_name": _plain(getattr(bank, "name", None), "Verified bank account"),
        "bank_institution_number": _plain(getattr(bank, "institution_number", None)),
        "bank_routing_number": _plain(getattr(bank, "transit_number", None)),
        "bank_account_number": _plain(getattr(bank, "account_number", None)),
    }


def render_loan_agreement(customer, loan) -> str:
    """Return HTML agreement text with variables substituted."""
    ctx = build_agreement_context(customer, loan)
    # Escape all scalar values used in the template body; schedule table is trusted HTML.
    safe = {
        key: (value if key == "amortization_schedule_table_small" else _esc(value))
        for key, value in ctx.items()
    }
    return AGREEMENT_HTML.format(**safe)


AGREEMENT_HTML = """
<article class="loan-agreement">
  <header class="party-block">
    <p><strong>LENDER:</strong> MohawkLoans (hereinafter referred to as "MohawkLoans")</p>
    <p><strong>BORROWER NAME:</strong> {customer_name}</p>
    <p><strong>BORROWER'S TELEPHONE NUMBER:</strong> {customer_phone_number}</p>
    <p><strong>ORIGINATION DATE:</strong> {date_today}</p>
    <p><strong>APPLICATION CHANNEL:</strong> {application_channel}</p>
  </header>

  <p>In this High-Cost Installment Loan Agreement ("Loan Agreement" or "Agreement"), the words "You" and "Your" mean the Borrower identified above. The words "We", "Us", "Our" and "Lender" mean MohawkLoans, a business operating on the Mohawk Territory of Kanehsatake in the Province of Quebec.</p>

  <p>You acknowledge that You have previously agreed to receive this Agreement, notices, disclosures and any other documents related to this Loan Agreement, to execute this Loan Agreement, and otherwise conduct this loan transaction with Us remotely through electronic means.</p>

  <h2>APPLICATION NOTICE</h2>
  <h2>JURISDICTION AND GOVERNING LAW NOTICE</h2>

  <p>MohawkLoans is a lending enterprise owned and operated from the Mohawk Territory of Kanehsatake.</p>
  <p>All loan applications, underwriting decisions, approvals, Loan Agreements, electronic signatures, records, and data relating to Your Loan are processed, executed, stored, and administered on systems located within the Mohawk Territory of Kanehsatake and under the authority of Kanehsatake.</p>
  <p>By submitting an application, electronically signing this Agreement, or otherwise continuing with this transaction, You acknowledge and agree that:</p>
  <ol class="lettered">
    <li>You are voluntarily seeking financial services from a business operating within the Mohawk Territory of Kanehsatake;</li>
    <li>Any Loan Agreement entered into between You and MohawkLoans is deemed to have been entered into, executed, and completed within the Mohawk Territory of Kanehsatake;</li>
    <li>Electronic signatures, records, communications, contractual documents, underwriting records, and loan-administration data are created, maintained, processed, and stored on infrastructure located within Kanehsatake;</li>
    <li>The lending relationship and this Agreement shall be governed by the applicable laws, regulations, customs, business practices, and lending standards recognized by the Mohawk Territory of Kanehsatake, together with any applicable federal laws that cannot lawfully be excluded;</li>
    <li>Any concern, complaint, dispute, request for review, or allegation relating to Your application, Loan, Agreement, payments, servicing, or collection activity must first be submitted to the Mohawk Fair Lending Practices Bureau or to such other dispute-resolution body as may be designated by Kanehsatake; and</li>
    <li>MohawkLoans operates pursuant to the inherent rights of the Mohawk people to conduct lawful economic activities within their territory. Nothing in this Agreement shall be construed as a waiver of any rights, privileges, protections, immunities, or jurisdictional positions available to Kanehsatake, its governing bodies, or enterprises operating under its authority.</li>
  </ol>
  <p>If You do not agree with this Jurisdiction and Governing Law Notice, You must not proceed with the application or sign this Agreement. By continuing, You expressly acknowledge and accept that Your application, this Agreement, and all related transactions are subject to the jurisdiction, laws, lending standards, and dispute-resolution processes of the Mohawk Territory of Kanehsatake, subject to any applicable laws that cannot lawfully be waived or excluded.</p>

  <p><strong>Covered Borrower Statement:</strong> You represent and warrant that YOU ARE (i) at least 18 years of age, (ii) not considered unfit or under guardianship and (iii) a legal resident of Canada. You understand that We will be making this loan in reliance on the truth of this statement.</p>

  <p><strong>Alternative Forms of Credit:</strong> This Loan has a high interest rate and is not intended to provide a solution for longer term credit or other financial needs. Alternative forms of credit may be less expensive and more suitable for your financial needs. Please consider Your ability to repay the loan and if You are having financial difficulties, You should seek the assistance of financial counselors. Please carefully read the terms of this Agreement before executing.</p>

  <p><strong>{funding_authorization_heading}:</strong> The Lender agrees to provide financing in the amount of {amount_financed} dollars ({amount_financed} CAD) (the "Principal Amount"). {funding_disbursement_terms}</p>

  <p>{funding_repayment_ack}</p>

  <p>Interest at the annual rate disclosed in this Agreement is calculated on the outstanding Principal Amount using a daily periodic rate equal to the annual rate divided by 365. The Principal Amount may consist of the {funding_principal_components} and financed third-party or cosigner service fees, if applicable. Interest begins to accrue on the Funding Date and continues until all principal, fees, and accrued interest are paid in full.</p>

  <p><strong>Consideration:</strong> The Lender and the Borrower agree on a payment plan of {loan_number_of_payments} of payments of {loan_first_payment_amount} dollars ($CAD) each. The payment period is effective upon signature of this contract and the first of these payments is due on {loan_first_payment_date}. The Payment Schedule is established as follows:</p>

  {amortization_schedule_table_small}

  <section class="example-box">
    <h3>ILLUSTRATIVE REPAYMENT EXAMPLE — EXAMPLE ONLY, NOT THE BORROWER'S ACTUAL PAYMENT SCHEDULE</h3>
    <p>The following calendar is provided solely to illustrate how financed fees and daily interest may affect repayment. It is not an offer, approval, promise, or statement of the Borrower's actual loan terms. The Borrower's binding Principal Amount, annual interest rate, Funding Date, payment frequency, payment amounts, fees, and due dates are those stated elsewhere in the completed Agreement and Payment Schedule.</p>
    <p><strong>Example assumptions:</strong></p>
    <ul>
      <li>{funding_example_disbursement_label}: $500.00</li>
      <li>Financed service/cosigner fee: 70% of $500.00 = $350.00</li>
      <li>Example Principal Amount: $500.00 + $350.00 = $850.00</li>
      <li>Example annual interest rate: 35.00%</li>
      <li>Example daily periodic rate: 35.00% ÷ 365 = 0.095890% per day</li>
      <li>Example payment frequency: every 14 days (biweekly)</li>
      <li>Number of example payments: 6</li>
      <li>Example first payment date: 2026-08-07</li>
      <li>Calculation method used for this illustration: interest for each period equals the opening outstanding principal multiplied by 35% ÷ 365 multiplied by 14 days. Each payment includes approximately one-sixth of principal plus the interest accrued for that period. Rounding is to the nearest cent.</li>
    </ul>
    <div class="table-wrap">
      <table class="schedule example">
        <thead>
          <tr>
            <th>Payment</th><th>Due Date</th><th>Days</th><th>Opening Balance</th><th>Interest</th><th>Principal</th><th>Total Payment</th><th>Closing Balance</th>
          </tr>
        </thead>
        <tbody>
          <tr><td>1</td><td>2026-08-07</td><td>14</td><td>$850.00</td><td>$11.41</td><td>$141.67</td><td>$153.08</td><td>$708.33</td></tr>
          <tr><td>2</td><td>2026-08-21</td><td>14</td><td>$708.33</td><td>$9.51</td><td>$141.67</td><td>$151.18</td><td>$566.66</td></tr>
          <tr><td>3</td><td>2026-09-04</td><td>14</td><td>$566.66</td><td>$7.61</td><td>$141.67</td><td>$149.28</td><td>$424.99</td></tr>
          <tr><td>4</td><td>2026-09-18</td><td>14</td><td>$424.99</td><td>$5.71</td><td>$141.67</td><td>$147.38</td><td>$283.32</td></tr>
          <tr><td>5</td><td>2026-10-02</td><td>14</td><td>$283.32</td><td>$3.80</td><td>$141.67</td><td>$145.47</td><td>$141.65</td></tr>
          <tr><td>6</td><td>2026-10-16</td><td>14</td><td>$141.65</td><td>$1.90</td><td>$141.65</td><td>$143.55</td><td>$0.00</td></tr>
        </tbody>
      </table>
    </div>
    <p><strong>Example totals:</strong></p>
    <ul>
      <li>{funding_example_disbursement_total_label}: $500.00</li>
      <li>Financed 70% service/cosigner fee: $350.00</li>
      <li>Total example Principal Amount: $850.00</li>
      <li>Total example interest over the six scheduled periods: $39.94</li>
      <li>Total of the six example payments: $889.94</li>
    </ul>
    <p class="important"><strong>IMPORTANT: THIS IS AN EXAMPLE ONLY.</strong> Actual interest is calculated daily on the actual outstanding balance and may differ if the actual Funding Date, due dates, number of elapsed days, payment frequency, payment amount, payment timing, fees, missed payments, deferrals, returned payments, or prepayments differ. The actual completed Payment Schedule controls.</p>
  </section>

  <h2>Special Remarks</h2>
  <p>The Borrower has the right to pay off the totality or part of his debt before the due date without notice or without incurring any early payment penalty.</p>
  <p>All payments will be applied first to fees due to Us, then to interest, and then to principal. All prepayments will be applied in the same order.</p>
  <p>If the Borrower were to miss or be late on one or any planned payment relative to this contract, the Lender has the right to demand the Borrower to pay the totality of the remaining balance of the loan, including interest and related expenses.</p>
  <p>A fee of sixty dollars ($60 CAD) will be charged to the Borrower in the case where a preauthorized payment would fail to be completed ("Dishonored Fees").</p>
  <p>A fee of two dollars and ninety-five cents ($2.95 CAD) will be charged to the Borrower for every debit transaction.</p>
  <p>A fee of five dollars ($5 CAD) will be charged to the Borrower for every Interac E-transfer transaction.</p>
  <p>A fee of forty ($40 CAD) dollars will be charged to the Borrower for any postponing of planned payments included in this contract ("Deferral Fee").</p>
  <p>The Borrower must notify the lender about postponing automatic payments at least four business days before the date of said payment.</p>

  <p><strong>Right of Rescission:</strong> You have the right to rescind this financing without incurring a fee if, on or before the close of the next business day following the Funding Date, You provide written notice to Us at {lending_license_holder_address} and {rescission_return_terms}</p>

  <h2>Automatic Payment Authorizations</h2>
  <p>By signing this Agreement, You authorize Your payments to Us via electronic fund transfers as indicated below by Pre-Authorized Debit (PAD) or as mutually agreed upon outside of this Loan Agreement.</p>

  <h3>Pre-Authorized Debit (PAD) Authorization</h3>
  <p>You hereby understand and agree that If You are approved, Your Loan payments will be due in accordance with the Payment Schedule in this Loan Agreement by Pre-Authorized Debit. As such, Your payments will be automatically initiated by Us in accordance with this Loan Agreement and You authorize Us to: (i) initiate recurring Pre-Authorized Debits for payments on Your Loan account from the Bank Account identified in the Pre-authorized Debit Agreement annexed to this Loan Agreement for the amounts indicated in the Payment Schedule above including applicable fees. (ii) You also authorize Us to initiate any corrective credit entries necessary to Your Bank Account and to re-initiate a debit entry for the same amount if the initial debit is unsuccessful, when applicable and as allowed by applicable law. (iii) You also authorize Us to charge You for all Dishonored and/or Deferral Fees that shall accumulate during the payment period of this Loan Agreement in a single installment at the end of the payment period indicated herein. You acknowledge that any transactions so authorized must comply with all applicable laws.</p>
  <p>You certify both that You are an authorized user of the Bank Account identified in the Pre-Authorized Debit Agreement and that such Bank Account is an open account in good standing. You authorize the financial institution identified herein to release to Us any and all information deemed necessary to our verification process. If there is any missing or erroneous information regarding Your bank, including its routing and transit number, or Your account number, or if You make a change to Your bank information, You authorize Us to verify and correct such information.</p>
  <p>You further certify that information given in connection with this Agreement is true and correct. You authorize Us to verify all of the information that You gave Us such as, income and bank account details as may be necessary to process Your request for a loan, determine Due Dates, and administer Your account with Us. You specifically authorize Us to use information You provided Us, including Your bank account number, to verify information related to Your bank account through telephone, electronic databases, or other electronically initiated bank records. You also give Us consent to obtain information about You from consumer reporting agencies or other sources in connection with Your request for credit, and at any time that You owe Us money under this or any Agreement.</p>
  <p>The Borrower therefore authorizes the Lender to make withdrawals according to the sums and dates agreed upon in this contract and the Pre-Authorized Debit (PAD) Agreement. The borrower agrees that the lender contact any person, employer, organism or financial institution concerning the obtaining of information relative to the loan.</p>
  <p><strong>Notice of furnishing negative information:</strong> We may report information about Your account to credit bureaus. late payments, missed payments, or other defaults on Your account may be reflected in Your credit report.</p>
  <p><strong>Bankruptcy:</strong> You certify to Us that You are not currently a debtor under any proceeding in bankruptcy and have not filed a petition for relief under any chapter of the Bankruptcy and Insolvency Act.</p>
  <p><strong>Agreement To Receive Notices Electronically:</strong> While You acknowledge and agree that You have previously agreed to electronic communications, You understand that if You would like to request a physical document be mailed to You, You may do so by written request to {lending_license_holder_address}. You agree that, You have previously provided to Us an electronic or email address and have otherwise consented to the electronic delivery of notices and disclosures and any notices may be delivered to You electronically, to the fullest extent permitted by applicable law.</p>

  <h2>Communications Consent</h2>
  <p>(a) By signing this Agreement, You authorize Us, Our assigns, successors, successors in interest and Our servicing agents (collectively hereinafter "Agents") to contact You at any telephone number, including any cellular/mobile telephone number, and email address(es) You have provided in the loan application and Agreement for non-marketing, account management purposes, including collection of any outstanding debt You may have with Us. Telephone numbers You authorize Us and Our Agents to text message will include the cellular/mobile telephone You provided Us on the loan application or Agreement as well as any numbers provided to Us or Our Agents at a later time with Your permission. You agree to pay any fee(s) or charge(s) that You may incur from third party communication service providers for incoming and outgoing messages from or to Us or Our Agents, without reimbursement from Us or them. You further agree to open and review all messages and contacts from Us in a confidential manner to ensure You are the only recipient.</p>
  <p>(b) Advertising, Marketing, and Telephone Communications/Messaging Consent: By opting-in to our Advertising, Marketing, and Telephone Communications/Messaging Policy below in this Agreement, You agree that You authorize Lender and Our Agents to contact You in any manner (including text messages, robocalls/robotexts, auto-dialed calls, direct drop voice mail service, apps or live chat) at the telephone number(s) and email address(es) You provided in the loan application and Agreement, to provide information on special sales or marketing offers, as well as reminders, notices, suspected fraud or identity theft, obtaining information necessary for Us to service Your account, collecting on Your account, notifying You as to important issues regarding Your account, notifying You of promotions, providing coupons or other marketing materials, any other reason allowed under the applicable Canadian Laws, and any other lawful purpose ("Messaging"). You further authorize Your wireless operator to disclose Your cellular/mobile number, name, address, email, network status, customer type, customer role, billing type, cellular/mobile device identifiers (IMSI and IMEI) and other subscriber and device details, if available, to Lender and service providers for the duration of the business relationship, solely for identity verification and fraud avoidance.</p>
  <p><strong>Detailed Wireless Policy:</strong> If You provide Us with authorization and opt-in to send You text messages, it is Your responsibility to provide Us with a true, accurate, and complete cellular/mobile number and to maintain and update promptly any changes in this information. At Our option, We may treat Your provision of an invalid mobile phone number, or the subsequent malfunction of a previously valid mobile phone number, as a withdrawal of Your consent to receive text messaging.</p>
  <p>By opting-in to Our Advertising, Marketing, and Telephone Communications/Messaging Policy You understand that u are opting-in to text messaging and You are providing Us with Your consent to use Your personal information to provide the services You have requested, including services that display customized content and advertising. Your provider's Msg &amp; Data Rates apply to any SMS (Short Message Service) messages. You may opt-out and remove Your SMS information by sending "STOP", "END", "CANCEL", "UNSUBSCRIBE" or "QUIT" to the SMS text message You have received. If You remove Your SMS information from Our database, Your number will no longer be used for secondary purposes, disclosed to third parties (if applicable) and used by Us for third parties to send promotional correspondence to You (if applicable). Data obtained from You in connection with this SMS service may include Your name, address, cell phone number, Your provider's name, the date and time, and content of Your messages. We will not be liable for any delays in the receipt of any SMS messages, as delivery is subject to effective transmission from Your network operator. SMS message services are provided on an AS IS basis.</p>

  <p><strong>Governing Law:</strong> MohawkLoans is a Mohawk lending enterprise owned and operated from the Mohawk Territory of Kanehsatake. This Agreement, the application process, underwriting decision, approval, electronic execution, recordkeeping, servicing, and administration of the Loan are deemed to occur within Kanehsatake. The parties intend that this Agreement be governed by the applicable laws, regulations, customs, business practices, lending standards, and dispute-resolution processes recognized by the Mohawk Territory of Kanehsatake, together with any applicable federal laws that cannot lawfully be excluded. The laws and lending standards applicable within Kanehsatake may differ from those of the province or territory where You reside. If You do not agree to enter into a Loan Agreement on this basis, You must not sign or proceed. This Agreement shall be deemed executed and completed within the Mohawk Territory of Kanehsatake. Nothing in this Agreement constitutes a waiver of any rights, privileges, protections, immunities, or jurisdictional positions available to Kanehsatake, its governing bodies, or enterprises operating under its authority.</p>

  <p><strong>Default:</strong> You will be in default under this Loan Agreement if any information You provide to Us is false or fraudulent in any material manner, if You provide a false signature to Us on any documents provided to Us, if You do not follow all the terms of this Loan Agreement or if You fail to repay the Loan and any accrued fees, charges and interest in accordance with the terms of this Loan Agreement. In the event of a default by You, all outstanding principal plus all accrued fees, charges and interest shall become immediately due and payable. If We waive Our right to seek immediate payment of all sums due and owing, such waiver shall be revocable by us at any time for any reason. Upon Your default, We have the right to exercise all of Our remedies to enforce payment in accordance with the terms of this Loan Agreement. In the event that You are in default, interest will continue to accrue in accordance with the terms of this Agreement. In the event that You are in default, as described herein, and to the fullest extent permitted under applicable law, You authorize Us or any collection agency which we employ or to which your Loan or the outstanding balance of your Loan has been assigned, to continue to submit debits in accordance with authorization You have provided via the Pre-Authorized Debit (PAD) Agreement against the account identified therein until such time as all amounts due and owing under this Loan Agreement have been satisfied in full.</p>

  <p><strong>Dispute Resolution:</strong> To encourage the prompt review and resolution of consumer concerns, the Borrower and Lender agree that any concern, complaint, controversy, request for review, allegation, claim, or dispute arising from or relating to the Borrower’s application, this Agreement, the Loan, payment processing, servicing, collection activity, or any related communication must first be submitted to the Mohawk Fair Lending Practices Bureau or to such other dispute-resolution body as may be designated by Kanehsatake. The parties shall follow the procedures, standards, and remedies established or recognized by that body before pursuing any other remedy, except where applicable law provides a right that cannot lawfully be waived, restricted, or delayed.</p>

  <p><strong>Class Action Waiver:</strong> You further agree that You will not bring, join, or participate in any class action claim, dispute or controversy You may have against Us or Our agents, servicers, directors, officers, and employees or any related third parties related to any dispute and where applicable, You also agree to opt out of any class actions against Us.</p>

  <p><strong>Our Policy Regarding Financial Privacy:</strong> By signing this Agreement, You acknowledge that You have reviewed and agree to Our Privacy Policy, which can be found at: {lending_license_holder_website}/privacy-policy/</p>

  <p><strong>Validity and Effectiveness:</strong> Wherever possible each provision of this Agreement will be interpreted in such a manner as to be effective and valid under applicable law. If any provision of this Agreement is prohibited by or invalid under applicable law, such provision will be ineffective to the extent of such prohibition or invalidity, but the remainder of such provision and the remaining provisions of this Agreement will not be invalidated.</p>

  <p><strong>Severability:</strong> If one or more provisions of this Agreement are held to be unenforceable under applicable law, the parties agree that such provision shall be excluded from this Agreement and the balance of the Agreement shall be interpreted as if such provision were so excluded and the balance of the Agreement shall be enforceable in accordance with its terms.</p>

  <p><strong>Survival:</strong> The provisions of this Agreement providing for the Dispute Resolution Procedure for all Disputes, as well as the Agreement Not to Bring, Join or Participate in Class Actions, shall survive repayment in full and/or default under this Loan Agreement.</p>

  <h2>Cosigners – Cosigns.ca</h2>
  <p>The Borrower acknowledges and agrees that, as part of the loan approval and risk mitigation process, the Lender may engage third-party cosigner facilitation services, including but not limited to Cosigns.ca (“Cosigner Service”). The purpose of such Cosigner Service is to support the approval of the Borrower’s loan application by introducing a qualified cosigner profile; however, such cosigner shall not be responsible for repayment of the Loan, and assumes no liability, obligation, or guarantee with respect to the Borrower’s repayment obligations under this Agreement.</p>
  <p>The Borrower hereby expressly authorizes the Lender to allocate and pay a cosigner service fee equal to up to seventy percent (70%) of the {funding_amount_label}, which fee may be financed and included in the Principal Amount. The Borrower understands and agrees that the Principal Amount may include both (i) {principal_funding_component} and (ii) any financed cosigner or third-party service fee. {principal_deposit_disclaimer}</p>
  <p>The Borrower further acknowledges that the engagement, compensation, and contractual relationship with any cosigner or Cosigner Service is solely between the Lender and such third party. The Borrower shall have no claim, recourse, or rights against the cosigner or Cosigner Service in relation to this Agreement.</p>
  <p>The Borrower may propose their own cosigner for consideration; however, the Lender reserves the sole and absolute right to accept, reject, or further verify any proposed cosigner in accordance with the Lender's underwriting, identity-verification, and risk requirements. Acceptance of a proposed cosigner does not alter the Borrower's obligations unless the Lender expressly agrees otherwise in writing.</p>

  <p class="caps"><strong>BY ELECTRONICALLY SIGNING BELOW, YOU ACKNOWLEDGE THAT THIS AGREEMENT CONTAINS ALL THE TERMS OF THIS AGREEMENT AND THAT YOU AGREE TO ALL THE TERMS OF THIS AGREEMENT, INCLUDING THE AGREEMENT TO FOLLOW THE DISPUTE RESOLUTION PROCEDURE PROVISION FOR ALL DISPUTES AND THE AGREEMENT NOT TO BRING, JOIN OR PARTICIPATE IN CLASS ACTIONS. YOU ALSO ACKNOWLEDGE A RECEIPT OF A FULLY COMPLETED COPY OF THIS LOAN AGREEMENT. YOU FURTHER ACKNOWLEDGE THAT THIS AGREEMENT WAS FILLED IN BEFORE YOU SIGNED IT, AND THAT YOU HAVE RECEIVED A COMPLETED COPY OF IT.</strong></p>
  <p>You agree that You have read, understand, and consent to Our Advertising, Marketing, and Telephone Communications and Messaging Policy.</p>
  <p class="caps"><strong>SIGNING THIS AGREEMENT DOES NOT OBLIGATE YOU TO ACCEPT THE LOAN. YOU ACKNOWLEDGE THE LENDER STILL NEEDS TO REVIEW AND APPROVE THE LOAN AGREEMENT, WHICH YOU ACKNOWLEDGE AND AGREE WILL OCCUR ON THE MOHAWK TERRITORY OF KANEHSATAKE, AS THE LAST ACT OF THIS CONTRACT CONSUMMATION AND YOU MAY WITHDRAW BEFORE THAT TIME.</strong></p>
  <p class="caps"><strong>BY SIGNING BELOW, YOU AGREE THAT YOU ARE ELECTRONICALLY SIGNING THIS AGREEMENT. IF YOU ARE SIGNING ELECTRONICALLY, YOU AGREE THAT YOUR ELECTRONIC SIGNATURE HAS THE FULL FORCE AND EFFECT OF YOUR PHYSICAL SIGNATURE AND THAT IT BINDS YOU TO THIS AGREEMENT IN THE SAME MANNER AS A PHYSICAL SIGNATURE.</strong></p>

  <h3>SIGNATURE — PLEASE SIGN YOUR NAME EXACTLY AS IT APPEARS ON YOUR APPLICATION</h3>
  <div class="sig-grid">
    <div>
      <p class="label">Your Full Name</p>
      <p class="sig-line">{customer_name}</p>
      <p class="label">Date</p>
      <p class="sig-line">{date_today}</p>
    </div>
    <div>
      <p class="label">Lender</p>
      <p class="sig-line">MohawkLoans</p>
      <p class="label">Date</p>
      <p class="sig-line">{date_today}</p>
    </div>
  </div>

  <hr />

  <h2>PRE-AUTHORIZED DEBIT (PAD) AGREEMENT</h2>
  <p><strong>FOR ACCOUNT NUMBER:</strong> {bank_account_number}</p>
  <p>I, the undersigned, authorize MohawkLoans (hereinafter "Payee") and the financial institution designated below to debit the account identified below to reimburse the Loan for the above numbered account as established in this PAD Agreement.</p>

  <h3>PAYOR INFORMATION — Account holder name and account number</h3>
  <div class="meta-grid">
    <p><span>Last and first name(s) of account holder(s)</span><strong>{customer_name}</strong></p>
    <p><span>Telephone No.</span><strong>{customer_mobile_phone}</strong></p>
    <p><span>The name of the financial institution where the account is located</span><strong>{bank_name}</strong></p>
    <p><span>Institution No.</span><strong>{bank_institution_number}</strong></p>
    <p><span>Transit No.</span><strong>{bank_routing_number}</strong></p>
    <p><span>Account No. (with check digit)</span><strong>{bank_account_number}</strong></p>
  </div>

  <h3>PAYEE INFORMATION — Contact information</h3>
  <div class="meta-grid">
    <p><span>Name of organization</span><strong>MohawkLoans</strong></p>
    <p><span>c/o or e-mail address</span><strong>{lending_license_holder_email}</strong></p>
    <p><span>Address (street, city, province)</span><strong>{lending_license_holder_address}</strong></p>
    <p><span>Telephone No.</span><strong>{lending_license_holder_phone}</strong></p>
  </div>

  <h3>Withdrawal authorization</h3>
  <p>I, the undersigned, (if a legal person, herein represented by its duly authorized representative(s)), authorize the Payee to make pre-authorized debits (PAD) from my account with the aforementioned financial institution, at the following interval:</p>
  <p><strong>{pay_frequency}</strong> beginning on <strong>{loan_pending_first_payment_date}</strong></p>
  <p>Each withdrawal will correspond to: a fixed amount of <strong>${loan_first_payment_amount} CAD</strong></p>

  <p><strong>Change:</strong> I, the undersigned, shall inform the Payee, in a timely manner, of any changes to this Agreement.</p>

  <h3>DECLARATION</h3>
  <p>I, the Undersigned, hereby waive the right to receive (i) 10-day pre-notification of the amount of the PAD prior to my first debit, (ii) pre-notification of each PAD (in the case of variable amount payments) and (iii) pre-notification for any changes to the amount of each PAD and/or (iv) pre-notification of any change to the payment date of the PAD.</p>
  <p>I, the undersigned, have certain recourse rights if any debit does not comply with this PAD agreement. For example, I have the right to receive a reimbursement for any debit that is not authorized or is not consistent with this PAD agreement. To obtain more information on my recourse rights, I may contact my financial institution or visit www.payments.ca</p>
  <p>I, the undersigned, may revoke my authorization at any time, upon providing four (4) days' notice, in writing, to {lending_license_holder_email}. I understand that I may obtain a sample cancellation form or further information on my right to cancel a PAD agreement at my financial institution or by visiting www.payments.ca.</p>
  <p>If I require more information or have an issue regarding my PAD agreement with Payee, I understand that I may contact you in writing at {lending_license_holder_address}.</p>
  <p><strong>Province of Quebec Only.</strong> It is the express wish of the parties that this agreement and any related documents be drawn up and executed in English. Les parties conviennent que la présente autorisation et tous les documents s’y attachant soient rédigés et signés en anglais.</p>
  <p>By signing this agreement, the Payor acknowledges having read and received a copy of this PAD agreement, acknowledges understanding the terms and conditions of this PAD agreement, and agrees to be bound by the terms and conditions of this PAD agreement. Payor represents and warrants that the person(s) whose signature(s) are required to sign on the Account have signed this PAD agreement. If only 1 signature is required for the Account, then only 1 Payor need sign. If more than 1 signature is required, all authorized signatories of Payor must sign.</p>

  <div class="sig-grid">
    <div>
      <p class="label">Payor Signature</p>
      <p class="sig-line">{customer_name}</p>
    </div>
    <div>
      <p class="label">Date</p>
      <p class="sig-line">{date_today}</p>
    </div>
  </div>
</article>
""".strip()
