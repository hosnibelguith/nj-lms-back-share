# LendStack API Documentation

## Base URL
```
Production: https://your-app.herokuapp.com/api
```

## Authentication
All endpoints (except login) require JWT token in header:
```
Authorization: Bearer <access_token>
```

---

## Auth Endpoints

### POST /api/auth/login/
Login and get JWT tokens.
```json
Request:
{
  "email": "agent@lendstack.com",
  "password": "password123"
}

Response:
{
  "access": "eyJ...",
  "refresh": "eyJ...",
  "user": {
    "id": "uuid",
    "email": "agent@lendstack.com",
    "full_name": "John Agent",
    "permission_level": 2
  }
}
```

### POST /api/auth/refresh/
Refresh access token.
```json
Request:
{ "refresh": "eyJ..." }

Response:
{ "access": "eyJ..." }
```

### GET /api/auth/me/
Get current user info.

### POST /api/auth/logout/
Logout (invalidate refresh token).

---

## Customers

### GET /api/customers/
List customers.
Query params: `?search=`, `?status=`, `?page=`, `?page_size=`

### POST /api/customers/
Create customer.
```json
{
  "first_name": "John",
  "last_name": "Doe",
  "email": "john@example.com",
  "phone": "+14165551234",
  "province": "ON"
}
```

### GET /api/customers/{id}/
Get customer details.

### PATCH /api/customers/{id}/
Update customer.

---

## Loans

### Loan Status Flow
```
pending → approved → contract_sent → contract_signed → funded → active → paid_off
                  ↘ declined                                         ↘ defaulted
```

### GET /api/loans/
List loans.
Query params: `?status=`, `?customer_id=`, `?type=`, `?search=`

### POST /api/loans/
Create loan.
```json
{
  "customer": "customer-uuid",
  "type": "nojuice",
  "principal": 500.00,
  "fee": 75.00,
  "bank_account": "bank-account-uuid",
  "notes": "First time customer"
}
```

### GET /api/loans/{id}/
Get loan with payments.

### POST /api/loans/{id}/approve/
Approve loan.
```json
{
  "bank_account_id": "uuid"  // optional
}
```

### POST /api/loans/{id}/decline/
Decline loan.
```json
{
  "reason": "High risk - too many NSFs"
}
```

### POST /api/loans/{id}/send_contract/
Send contract to customer (triggers DocuSign).

### POST /api/loans/{id}/fund/
### POST /api/loans/{id}/funding/initiate/
Initiate processor funding for a signed loan. The loan remains `pending_funding` until a ZūmRails `Transaction.Completed` webhook is received.
```json
{
  "method": "etransfer",
  "schedule_confirmed": true,
  "funding_destination": {
    "email": "customer@example.com"
  },
  "collections_account_id": "bank-account-uuid"
}
```

Validation:
- `loan.status` must be `pending_funding`
- funding destination is required
- collections account is required
- `schedule_confirmed` must be true
- existing `processing` or `completed` funding blocks duplicate funding

Retries are allowed after `failed` or `returned` funding attempts and create a new `FundedPayment`.

### GET /api/loans/{id}/funded-payments/
List funding attempts for a loan.

### GET /api/loans/{id}/funding/options/
Return funding modal data, including today's advisory funding method recommendation.

### /api/funding-method-recommendations/
Staff CRUD endpoint for weekday funding recommendations.
```json
{
  "weekday": 0,
  "method": "eft",
  "is_active": true,
  "notes": "Monday EFT recommendation"
}
```

### POST /api/loans/{id}/collections/initiate/
Initiate an EFT collection through ZūmRails.
```json
{
  "amount": 100.00,
  "payment_id": "scheduled-payment-uuid"
}
```

Collections always use EFT. A ZūmRails `Transaction.Completed` webhook starts the 4-business-day settlement period but does not complete the payment.

### GET /api/loans/{id}/collection-payments/
List processor collection attempts for a loan.

### PATCH /api/loans/{id}/collections-account/
Change the future collections account after a failed EFT collection for the same loan.
```json
{
  "bank_account_id": "new-bank-account-uuid",
  "failed_payment_id": "failed-collection-payment-uuid"
}
```

Creates an immutable collections account change audit record.

### POST /api/loans/settlement/process/
Run due collection settlement processing. Completed Zūm collections become internal completed payments only after 4 business days without failure/return/reject events.

### POST /api/loans/{id}/record_payment/
Record a manual payment.
```json
{
  "amount": 100.00,
  "type": "manual",  // or "etransfer"
  "reference": "EMT-12345",
  "notes": "Customer paid via e-transfer"
}
```

### POST /api/loans/{id}/mark_defaulted/
Mark loan as defaulted.

### GET /api/loans/stats/
Dashboard statistics.
```json
Response:
{
  "total_funded": 50000.00,
  "total_balance": 25000.00,
  "pending": 5,
  "approved": 3,
  "active": 20,
  "defaulted": 2
}
```

---

## Payments

### GET /api/payments/
List payments.
Query params: `?loan_id=`, `?status=`

### POST /api/payments/
Create scheduled payment.
```json
{
  "loan": "loan-uuid",
  "amount": 100.00,
  "scheduled_date": "2024-01-15",
  "type": "scheduled"
}
```

### POST /api/payments/{id}/complete/
Mark payment as completed.

### POST /api/payments/{id}/fail/
Mark payment as failed.
```json
{ "reason": "Declined by bank" }
```

### POST /api/payments/{id}/nsf/
Mark payment as NSF.

### POST /api/payments/{id}/cancel/
Cancel scheduled payment.

---

## ZūmRails Webhooks

### POST /api/webhooks/zumrails/
Processes signed ZūmRails webhook payloads.

Required header:
```
zumrails-signature: <base64 hmac-sha256 signature>
```

Signature input is the raw request body and the secret is `ZUMRAILS_WEBHOOK_SECRET`.

Supported webhook types:
- `Transaction`
- `TransactionEvent`

Funding:
- `Transaction.Status = Completed` marks `FundedPayment.completed` and moves the loan to `active`.
- `Status` containing `Failed` marks funding failed.
- `Status = Returned` marks funding returned.

Collections:
- `Transaction.Status = Completed` stores `zum_status=Completed` and starts settlement.
- Failure, returned, and rejected events mark the collection failed immediately.

---

## Banking

### GET /api/bank-accounts/
List bank accounts.
Query params: `?customer_id=`

### POST /api/banking/connect/
Initialize Flinks connection.
```json
{
  "customer_id": "uuid",
  "redirect_url": "https://frontend.com/callback"
}
```

### GET /api/bank-transactions/
List transactions.
Query params: `?account_id=`, `?start_date=`, `?end_date=`

### GET /api/financial-reports/
List analysis reports.
Query params: `?bank_account_id=`

---

## Webhooks

### POST /api/webhooks/flinks/
Flinks webhook for bank data sync.

### POST /api/webhooks/contracts/
DocuSign webhook for contract status.

### POST /api/webhooks/twilio/
Twilio webhook for SMS status.

---

## Error Responses
```json
{
  "error": "Error message",
  "detail": "Detailed error info"
}
```

Status codes:
- 400: Bad Request (validation error)
- 401: Unauthorized (missing/invalid token)
- 403: Forbidden (permission denied)
- 404: Not Found
- 500: Server Error
