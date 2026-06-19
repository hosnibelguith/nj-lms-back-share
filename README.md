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
- Institution numbers `621` and `623` are rejected.
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
