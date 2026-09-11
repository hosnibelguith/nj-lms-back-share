# Payment processor

An optional Django/Next.js module restricted on both sides to the active staff account `mohamed+aclms@icollector.ai`. Other users, including other superusers, cannot access the plugin API or its Django admin models. Existing LMS funding, collection, banking, email, and webhook handlers are unchanged by this module.

## Plug / unplug in Django admin

Open `/admin/` as the owner, then **Payment processor → Plugin switch → Payment processor**.

- **Enabled** plugs the module in. Clearing it immediately blocks its API and pauses its dedicated worker. The frontend removes the sidebar entry at the next access refresh (up to 30 seconds).
- **Allow provider writes** permits reviewed actions only when the server also sets `PAYMENT_PROCESSOR_ALLOW_WRITES=True`. An existing server-level `ZUMRAILS_TRANSACTIONS_ENABLED=False` remains a hard block. The local review settings retain that block.
- **Reconcile minutes** controls complete background refreshes (minimum effective interval: two minutes; default five).
- **Read existing webhooks** enables an optional read-only bridge to already-received `loans.WebhookEvent` rows. It never changes those records or invokes the LMS payment handlers.

The database migration creates the switch disabled. Enable it for the owner's review when deploying. Disabling the switch keeps snapshots and action history for rollback.

## Host integration

Backend changes outside this folder are limited to optional app registration in `config/settings.py`, optional URL inclusion in `config/urls.py`, a dedicated `processor` process in `Procfile`, and an isolated test-settings shim. The plugin has no foreign keys to LMS models. Its only runtime host adapters read API Integrations settings, tenant identity, and optionally existing webhook events.

Frontend changes outside `components/payment-processor/` and `app/dashboard/payment-processor/` are the sidebar entry and exporting the existing authenticated request/download helpers. Browser requests go to Django; ZUM credentials and provider bearer tokens never reach Next.js.

`PAYMENT_PROCESSOR_INSTALLED=False` removes the Django app/routes/admin registration on restart. Scale its worker to zero before doing this. The frontend hides the entry when the availability endpoint is absent. To physically remove it, remove its sidebar import/component, its app page/component folders, its optional backend registration/URL/process lines, and this Django app. Do not drop tables just to unplug it. If permanent data deletion is desired, back them up and reverse this app's migrations before uninstalling it.

## Data and report behavior

- Wallet balances: ZUM GET, at most 15 seconds cached, refreshed by the visible UI every 30 seconds.
- Transactions, users, funding sources, wallet ledger: durable full pagination jobs. Every page must have the advertised count, unique IDs, and the expected page number. A failed/partial job never replaces the last complete report.
- Reports: AP/AR/wallet/card-funding flows; attempted/completed/failed volume; counts and success rate; average/max; daily/weekly/monthly volume; methods and status breakdowns; failure events; top users; refund/reversal/return classifications; processing times where ZUM provides timestamps. Currency totals stay separate. Date filtering uses `CreatedAt` in the selected timezone (UI: America/Toronto). These definitions may differ from individual ZUM portal widgets; portal numerical parity has not been certified.
- Export: all matching rows in a complete snapshot, not only the displayed page, with CSV formula escaping.
- Details: current transaction details and event timeline; user, bank status and available methods; funding sources; card balances/status/activity; optional invoice events and subscription details.
- Actions: AP/AR EFT/Interac, wallet fund/withdraw, supported card-funding flows; cancel, credit-card refund, authorization completion; user create/profile/bank/billing-address/delete; CSV validate/process; card activate/status/program/reissue. Every submission needs an expiring reviewed payload and typed confirmation. Identical recent previews share one operation; submitted or uncertain operations cannot be replayed. No full CSV, account number, card security code, or Interac answer is stored in the operation audit.
- Cards use documented APIs for the customer account. The plugin does not display PAN/CVV or install the ZUM sensitive-card SDK. Card reports optionally enumerate every user and card and fetch every activity page before publishing account-wide debit/decline reports and active-card counts. Debit totals include applicable fees and withdrawals; credit entries are included in exports. Optional invoice/subscription sections support retrieval, invoice events, and subscription pause/resume. Product creation and invoice/subscription creation are not included because the supplied requirements make those modules conditional on actual use.

Reports cover data returned by the supported API. The module does not call undocumented `/reports/*` or customer underwriting/payment-rail configuration endpoints. Missing provider capabilities return their actual HTTP status; no synthetic values are displayed. Full production backfill and live mutation execution were not run during implementation at the owner's request.

Snapshots, masked provider payloads, sync runs, webhook inbox and action history use only `payment_processor_*` tables on the default database. Their scope incorporates tenant database identity and ZUM URL/username. Banking/card identifiers are masked and secrets/security codes removed before persistence. Two complete core snapshots per resource are retained; optional card snapshots remain for audit. Live reads do not update LMS loans or collection/funding records.

## Run locally

Use the existing local review database and login email `mohamed+aclms@icollector.ai` with the unchanged local password.

Terminal 1, from `nj-lms-back`:

```powershell
.\.venv\Scripts\python.exe -B manage.py migrate --settings=config.settings_local_review
.\.venv\Scripts\python.exe -B manage.py runserver 127.0.0.1:8010 --settings=config.settings_local_review
```

Terminal 2, from `frontend-ui-redisgn`:

```powershell
npm run dev:review
```

When ready to import real ZUM data locally, terminal 3 from `nj-lms-back`:

```powershell
.\.venv\Scripts\python.exe -B manage.py payment_processor_worker --settings=config.settings_local_review
```

Starting that worker performs real API reads and stores masked customer/financial records locally. It is not started automatically by the web server. Open `http://localhost:3000/dashboard/payment-processor`, then use **Synchronize ZUM**. The worker continuously reads existing webhook events and reconciles core resources. **Card reports**, **Invoices** and **Subscriptions** are opt-in: click Synchronize while viewing the respective section. Card synchronization can take longer because it checks every user and observes ZUM request limits. For multiple database tenants, start one dedicated worker per configured alias with `--tenant <alias>`.

For a one-time complete core synchronization, add `--once`. This exits nonzero if a resource fails. All requests have timeouts and a shared per-account request budget; 429 responses defer jobs instead of creating retry storms. No existing Celery worker/beat is needed for this plugin.

## Release / rollback

Before release, back up production PostgreSQL and record each app's current Heroku release. Deploy only the isolated `payment-processor-owner-20260911` branches prepared from backend `8923298` and frontend `ce914fc`. Existing local funding-review work is not in those release branches. Heroku's existing release process applies migrations.

After an authorized deployment: enable the plugin switch, start the backend's dedicated `processor` process, and review synchronization completeness. Keep provider writes disabled until the owner chooses to enable them. Scaling the `processor` process can incur a Heroku dyno charge.

To pause: clear **Enabled** in Django admin. To roll back the feature: scale `processor=0`, disable the optional app registration, and restore the recorded frontend/backend releases. Additive plugin tables can remain for audit/rollback; other application tables require no schema rollback for this plugin.

Clean pulled-source ZIPs, Git bundles, pre-pull working copies, patches, and the pre-plugin local database backup are under `../backups/payment-processor-20260911-185302/` in the original workspace. `manifest.json` records both pulled hashes and the preserved local-work stash references. Restore into a new directory first so that later local work is not overwritten.

## Automated checks

```powershell
python -B manage.py test payment_processor.tests --settings=config.settings_payment_processor_test --noinput
```

These tests use an isolated in-memory database and block real HTTP. They cover owner/admin authorization, unplugging, pagination completeness, report math/dates/currencies, CSV escaping, signed webhooks, bank/card masking, review expiry, changed-payload rejection, batch validation matching, card ownership, and action replay/unknown-outcome protection.

References: [transactions](https://docs.zumrails.com/api-reference/transactions), [users](https://docs.zumrails.com/api-reference/users), [wallets](https://docs.zumrails.com/api-reference/wallets), [funding sources](https://docs.zumrails.com/api-reference/funding-source), [cards](https://docs.zumrails.com/api-reference/cards), [webhooks](https://docs.zumrails.com/api-reference/webhooks).
