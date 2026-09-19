# Mohawk iCollector integration

## Controls

Django admin: **iCollector > Plugin switch > Enabled**. The switch is initially
off. Only Mohawk superusers can administer the integration. Disabling pauses
capture, queued delivery, and dashboard polling, hides the sidebar entry, and
denies access to the cached dashboard. An HTTP request already sent can finish.
Enabling again recovers failures received since the first activation, including
those received while paused. Daily Rejects remains available independently.

**Reject deliveries** lists delivery state and safe errors. Invalid rows stop in
**Needs review** rather than retrying unchanged. Correct the customer data, select
the blocked rows, and use **Retry selected rows after correcting source data**.
This retains the frozen payment amount, balance, return date, and idempotency key.
Transient errors retry automatically, with a delay from 30 seconds to one hour.

## Backend configuration

- `ICOLLECTOR_BASE_URL=https://app.icollector.ai`
- `ICOLLECTOR_API_KEY` and `ICOLLECTOR_HMAC_SECRET`: backend secret store only
- `ICOLLECTOR_PROXY_URL`, falling back to Heroku `FIXIE_URL`

Oceon must enable its Mohawk integration and allowlist the proxy's **outbound**
addresses. Proxy DNS ingress addresses are different. The proxy applies only to
this client. Existing payment, banking, and other integrations retain their own
network configuration.

The existing Celery worker handles immediate post-commit delivery. Existing beat
runs `icollector.tasks.tick` every 30 seconds for recovery and a shared dashboard
cache. The integration uses only the default Mohawk database and lender slug
`mohawkloans`. Authenticated Mohawk staff read `/api/icollector/availability/` and
`/api/icollector/dashboard/`. These are read-only cache endpoints with private,
no-store responses. No payment provider is called by this integration.

New payment failures create one durable delivery per CollectionPayment UUID.
Financial fields are frozen after failure balance adjustments commit, then
retried with that same UUID and body. Reconciliation recovers bulk updates or
temporary signal/broker failures; a recovered record uses the balance available
when it is captured. Dashboard failures retain the last successful snapshot;
the UI shows stale data and isolated widget errors.

## Historical import

Preview one Toronto return date without writes:

```sh
python manage.py icollector_backfill --date 2026-09-14
```

Review the count, then queue exactly that preview using its fingerprint:

```sh
python manage.py icollector_backfill --date 2026-09-14 --apply --fingerprint <preview-fingerprint>
```

This exports existing rejection records; it does not rerun payments. Historical
imports use the current loan balance at capture time, since these records do not
contain a historical balance snapshot. Rerunning the command does not duplicate
existing deliveries. The existing Daily Rejects route remains
`/dashboard/collections`; iCollector is `/dashboard/collections/icollector`.

## Verification and rollback

```sh
python manage.py test icollector accounts.tests.test_arrive_integration accounts.tests.test_api_workflows loans.tests_pending_id --settings=config.settings_icollector_test --noinput
python manage.py makemigrations icollector --check --dry-run --settings=config.settings_icollector_test
```

Tests use isolated SQLite and mocked external HTTP, email and storage settings.
Deployment migrations only add this app's tables. Before release, retain source
bundles and a Heroku Postgres backup. First disable the plugin to pause outbound
work, then roll back application releases if necessary. Preserve the additive
tables and delivery receipts to keep duplicate protection. A code rollback does
not remove rows already exported to Oceon. Do not restore the whole production
database for a simple application rollback.
