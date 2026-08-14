# LendStack Django API (pairs with frontend-ui-redisgn-share)

## Customer portal flow

```
Apply → Banking (Flinks IBV) → Contract → optional Job & References → Dashboard
```

| Stage | Frontend | API |
|-------|----------|-----|
| Apply | `/apply` | `POST /api/portal/signup/start/`, `POST /api/portal/signup/verify-phone/` |
| Banking | `/customer/banking` | `POST /api/banking/connect/`, `GET /api/portal/me/banking/` |
| Contract | `/customer/contracts` | `GET /api/portal/me/contract-preview/`, `POST /api/portal/me/sign-contract/` |
| Job & References (optional) | modal on dashboard | `GET/PATCH /api/portal/me/job-references/` |
| Dashboard | `/customer/loans` | `GET /api/portal/me/dashboard/` |

### Onboarding stage transitions

| Event | `onboarding_stage` | `banking_verified` |
|-------|---------------------|-------------------|
| Signup complete | `banking_verification` | `false` |
| Flinks sync success | `contract` | `true` |
| Contract signed | `portal_active` | `true` |
| Flinks sync failure | `banking_verification` | `false` |

New customers are created with `status=pending`.

### Banking business rules

- Success requires at least one account and total transaction count > 0 across **all** accounts.
- Institution numbers `621`, `623`, and `703` are saved normally; agents may decline with “Unsupported bank” and see a funding warning.
- On failure: `sync_status=failed`, activity logged, retry email sent (`Action Required: Please reconnect your bank account`).
- Flinks holder `Email` / `PhoneNumber` saved to `User.flinks_email` / `User.flinks_phone`.

### Contract rules

- Contract endpoints return `403` until `banking_verified=true`.
- After signing, API returns `show_job_references_prompt: true` for the post-contract popup.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py createsuperuser   # staff login for /login
python manage.py seed_loans        # optional demo data
python manage.py runserver
```

API base URL: `http://localhost:8000/api`  
Frontend expects: `NEXT_PUBLIC_API_URL=http://localhost:8000/api` (default in frontend)

## Heroku

1. Create app and Postgres add-on.
2. Set config vars: `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=False`, `DJANGO_ALLOWED_HOSTS`, `DATABASE_URL`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS`.
3. Deploy — release phase runs migrations automatically.
4. Create a production admin account:
   ```bash
   heroku config:set BOOTSTRAP_ADMIN_EMAIL=you@example.com BOOTSTRAP_ADMIN_PASSWORD='your-strong-password' -a nj-lms-back
   heroku run python manage.py ensure_admin -a nj-lms-back
   ```
   Staff login: `POST /api/auth/login/` (frontend `/login`). Django admin: `/admin/`.

## Ops: heal payment schedule (keep Pending)

`heal_schedule_keeping_pending` rebuilds broken **Scheduled** / failed / nsf installments while leaving in-flight **Pending** (and processing collection) payments untouched. Default is **dry-run** (simulate only); pass `--apply` to write.

| Flag | What it does |
|------|----------------|
| `--loan-id` | Loan UUID to heal (required) |
| `--payment-amount` | Target installment amount for new scheduled rows |
| `--number-of-payments` | Alternate mode: number of new scheduled installments |
| `--frequency` | `weekly`, `bi-weekly`, or `monthly` (default `bi-weekly`) |
| `--start-date` | First new scheduled date `YYYY-MM-DD` (default: after last Pending) |
| `--reprice` | Recompute `loan.total_amount` with Adjust Schedule interest math, then schedule the remainder |
| `--apply` | Persist changes (without this flag: simulate only) |
| `--notes` | Optional note on the activity log when applying |

**Local**

```bash
# Simulate — prints KEEP / DELETE / CREATE; no DB writes
python manage.py heal_schedule_keeping_pending \
  --loan-id <uuid> --payment-amount 147.18 --frequency bi-weekly --start-date 2026-08-20

# Apply after reviewing the plan
python manage.py heal_schedule_keeping_pending \
  --loan-id <uuid> --payment-amount 147.18 --frequency bi-weekly --start-date 2026-08-20 --apply
```

**Heroku** — quote the whole Django command so Heroku CLI does not treat Django flags as its own:

```bash
# Simulate
heroku run "python manage.py heal_schedule_keeping_pending --loan-id 54fe7b2b-e32f-4883-b902-e2506c308875 --payment-amount 147.18 --frequency bi-weekly --start-date 2026-08-20" -a nj-lms-back

# Apply
heroku run "python manage.py heal_schedule_keeping_pending --loan-id 54fe7b2b-e32f-4883-b902-e2506c308875 --payment-amount 147.18 --frequency bi-weekly --start-date 2026-08-20 --apply --notes 'Ops heal keep Pending'" -a nj-lms-back
```

## Ops: re-pull pending IBV

`repull_pending_ibv` re-queues Flinks GetAccountsDetail for customers still stuck on IBV (failed / pending / inactive LoginId after a new application). Uses the stored LoginId — no new Connect. Default is **dry-run**; pass `--apply` to enqueue Celery tasks.

| Flag | What it does |
|------|----------------|
| `--since` | Only connections updated on/after `YYYY-MM-DD` |
| `--customer-id` | Limit to one customer |
| `--include-syncing` | Also re-queue rows currently marked `syncing` |
| `--limit` | Cap how many customers to queue |
| `--apply` | Enqueue re-pulls (without this flag: print only) |

```bash
# Local dry-run / apply
./scripts/repull_pending_ibv.sh
./scripts/repull_pending_ibv.sh --since 2026-08-14 --apply

# Heroku — quote the Django command
heroku run "python manage.py repull_pending_ibv --since 2026-08-14" -a nj-lms-back
heroku run "python manage.py repull_pending_ibv --since 2026-08-14 --apply" -a nj-lms-back
```
