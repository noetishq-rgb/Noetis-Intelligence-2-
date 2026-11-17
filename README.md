# NOETIS Intelligence

An internal marketing-intelligence dashboard with a real-data Brand Brain onboarding API, versioned human review and tenant-isolated PostgreSQL storage.

This repository combines the existing frontend, FastAPI backend, all 35 migrations, strict output schemas, tests, deployment configuration and bounded workers. Production secrets, database dumps, private review downloads and uploaded briefs are excluded.

## Deployment architecture

- **GitHub**: this source repository, CI tests, and a signed scheduled/manual invocation of the hosted public-capture worker. Research/planning workers remain explicitly disabled until configured.
- **Supabase PostgreSQL**: private `mi`, `identity` and `mi_meta` schemas, custom API/worker logins, forced row-level security and immutable artifacts. Browser code never receives database credentials or a service-role key.
- **Vercel**: the Python FastAPI application and static frontend on the same origin. The entrypoint is `vercel_entry:app` and the initial landing view is Brand Brain.

The application keeps its existing JWT/Argon2 authentication and brand permissions. It does not replace them with Supabase Auth or reuse the users/tables of an older dashboard implicitly. Supabase's `public` schema can contain an earlier application and is not part of this migration history.

## Owner and client access

Use `/owner` for the private Owner Console and `/login` for client sign-in. The owner issues a dedicated login and empty workspace; clients choose their own password and supply their brand details themselves. Returning browser sessions use a Secure HttpOnly cookie, and resets/suspensions revoke existing access. No provider call is needed to complete onboarding. See the [operating guide](docs/CLIENT_ACCESS_GUIDE.md).

## What live data means

Brand Brain uses authenticated server drafts, actual crawl/research job status, recorded source snapshots, finding reviews, comments and confirmed versions. It never fills its intelligence screens from sample dashboard fixtures.

The other marketing dashboard views retain clearly labeled illustrative performance data until their reporting integrations are connected. Deploying the infrastructure alone does not establish real Meta spend, Search Console rankings, email results or revenue. No paid provider is enabled by default. The SuperYou pilot has a separate bounded public-capture worker.

## Setup

1. Use a Python 3.12 environment and install `pip install -r requirements-dev.txt`. Node 22 runs the dependency-free frontend checks.
2. Connect the selected Vercel project to this GitHub repository. Keep production environment variables separate from previews. `.env.production.example` lists names; configure real secrets in the hosting provider, not the repository.
3. For a **new, reviewed Supabase application namespace**, use its administrator's direct/session-pooler PostgreSQL connection as `MI_MIGRATION_DATABASE_URL`, then run:

   ```sh
   python scripts/migrate.py --profile supabase
   ```

   Do not run this against an unrelated project. Existing native-profile migration records cannot silently be reinterpreted as managed records. The runner records original and effective SQL digests, creates no-login guard roles, grants only the policies needed by existing guard functions, and supports Supabase's `extensions` placement of pgcrypto. Its ledger lives in `mi_meta`; digest wrappers stay in `mi`. Existing public schema tables and privileges are preserved. The original SQL migration files are unchanged. `deployment/supabase_private_bootstrap.sql` is a fresh-install snapshot of the same managed operations and ledger hashes for the Supabase migration tool; do not apply both entry paths to an already initialized namespace.
4. Create the restricted API login using `scripts/provision.py runtime-role --name mi_runtime --kind api`. Its password is accepted via the existing hidden prompt or environment. Store that login's Supabase direct/session connection URL in Vercel as **`MI_DATABASE_URL`**, and set a random **`MI_JWT_SECRET`**. Do not deploy the administrative URL, Supabase service-role key or a BYPASSRLS role as the runtime.
5. Configure an email-bound, short-lived founder activation using the server-only `MI_BOOTSTRAP_EMAIL`, `MI_BOOTSTRAP_TOKEN_SHA256`, `MI_BOOTSTRAP_EXPIRES_AT` and `MI_BOOTSTRAP_ORGANIZATION` variables. Generate a random token, store only its SHA-256 digest in Vercel, and privately hand the founder `/#brandbrain?activate=TOKEN&email=EMAIL&name=NAME`. The fragment is removed immediately and the founder chooses a password in the app; activation is single use. Remove bootstrap variables after use. Alternatively use `scripts/provision.py founder --email ... --name ... --organization ...` with its hidden password prompt. `MI_ALLOW_SIGNUP` stays false.
6. Deploy the tested commit through Vercel. `/health` verifies that a safe API database role is available. Sign in with the application's founder account and create the real pilot workspace.
7. Provision separate bound service identities for that brand using `crawl-service`, `research-service` and `planning-service`. Store the respective URLs and UUIDs in GitHub encrypted secrets. The existing provisioner prevents an API login from becoming a worker service.
8. Enable only the worker repository variables you intend to run. `Bounded research workers` supports manual dispatch and a five-minute schedule. Each worker uses durable database leases, drains currently available work for a bounded period and stops. Schedules can be delayed by GitHub; immediate runs can be dispatched manually.

For local development, use `uvicorn vercel_entry:app --host 127.0.0.1 --port 8000`. This runs the same origin and role checks as deployment. Migrations and infinite worker loops do not execute inside web requests. Public capture can run one bounded job per hosted invocation using a dedicated brand-bound login. The scheduled invoker uses GitHub OIDC and contains no database secrets.

## GitHub worker configuration

| Kind | Encrypted secrets | Enable variable |
|---|---|---|
| Crawl | `NOETIS_CRAWL_DATABASE_URL`, `NOETIS_CRAWL_SERVICE_USER_ID` | `NOETIS_WORKERS_ENABLED=true` |
| Research | `NOETIS_RESEARCH_DATABASE_URL`, `NOETIS_RESEARCH_SERVICE_USER_ID`, `NOETIS_ANTHROPIC_API_KEY` | `NOETIS_RESEARCH_WORKER_ENABLED=true` |
| Planning | `NOETIS_PLANNING_DATABASE_URL`, `NOETIS_PLANNING_SERVICE_USER_ID`, `NOETIS_ANTHROPIC_API_KEY` | `NOETIS_PLANNING_WORKER_ENABLED=true` |

Paid generation additionally needs explicit `NOETIS_ALLOW_MODEL_CALLS=true`, provider/model selection, dated positive prices and a cost cap in paise. Planning also needs `NOETIS_ALLOW_PLANNING_CALLS=true`. Names are in `.github/workflows/workers.yml`. No default zero-price accounting or guessed spending budget is applied. The API's matching provider-readiness settings belong in server-only Vercel variables as well. Existing provenance, semantic approval, dependency and spending gates still apply.

Use Supabase **direct or session pooling** for bound worker logins. Verify `session_user` is the actual custom role. Do not substitute the anonymous/service-role HTTP API key or a transaction pooler with changed session identity.

## Verification

```sh
npm test
npm run build
python -m pytest -q
```

CI creates a disposable PostgreSQL 17 database with a non-superuser managed migration owner, applies and replays all migrations, provisions a non-bypass API login, and runs the backend suite. Test credentials in CI are deliberately synthetic and are not deployment defaults.

Operational details: [Brand Brain](docs/BRAND_BRAIN.md), [security](docs/SECURITY.md), [research operations](docs/RESEARCH_OPERATIONS.md), [planning API](docs/PLANNING_API.md). Current contract: `docs/openapi-brand-brain.json`.

## Website capture pilot

An owner can queue public capture from a Brand Brain session using the official website URL, even while client onboarding is a draft. `/sessions/{session_id}/capture` is versioned and idempotent; `/v1/brands/{brand_id}/brain/capture/run` runs one bounded pass for an owner. GitHub schedules the same service through `/internal/crawl`, which requires a verified GitHub RS256 token for this exact repository, main branch, workflow and audience. The configured worker has a separate `mi_app` login bound to one brand, and runs no model calls. Source pages, hashes, crawl errors and profile observations persist in Supabase.

Production variables: `MI_CRAWL_WORKER_DATABASE_URL`, `MI_CRAWL_SERVICE_USER_ID`, `MI_CRAWL_BRAND_ID`, `MI_CRAWL_SCHEDULER_ENABLED`. The first pilot is SuperYou. More brands require their own bound worker credentials. Other GitHub worker secrets in the table above apply only to the separately configured research/planning jobs or standalone legacy crawler.

Client category/objectives remain blank until supplied. The client must submit setup before an owner organizes the captured evidence into a review draft. Website statements are attributed observations; market, audience, budget and strategy claims stay unverified. Model provider credentials, pricing, cost caps and semantic review are separate requirements for broader research. Email automation remains on hold.
