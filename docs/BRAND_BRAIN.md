# Brand Brain onboarding — implementation and operations

17 November 2026. Backend 0.4.1 / UI 0.4.2. The wider Phase 5/6 dashboard rollout remains deferred. See BRAND_BRAIN_INTEGRATION_AUDIT.md for the audit completed before production code.

## What is implemented

Six connected screens in the existing shell: four-group setup, persisted research progress, six-chapter overview, reusable evidence/finding chapter, collaborative review, and versioned partial/full confirmation. The dark palette and serif editorial treatment apply only inside Brand Brain. Existing navigation, sample dashboard, charts, scenario tools and exports remain intact.

The current Sites publication is a static private design review. It is not the application backend. The journey never simulates sources, progress, a research result, a comment, or a successful confirmation. Its disconnected setup saves the four input groups on the device and explains that a backend account is required to submit. Screens requiring real data are gated until the server provides it.

## Reused infrastructure and new storage

Existing JWT login/refresh/logout, organization/brand memberships, forced RLS, brand/profile/intake records, durable crawl/research jobs, source snapshots, model/provider limits, semantic review and audit chains remain authoritative. No second crawler, LLM provider, prompt engine, job queue, notification transport or evidence registry was added.

Migrations 026–030 add seven forced-RLS tables: onboarding sessions, immutable Brand Brain versions, append-only finding reviews, threads, comments, thread events and confirmations. Links/objectives are typed session fields; chapters/findings are typed JSON version content. Job stages are an API projection of actual stored jobs/evidence. `research_jobs` maps to existing `jobs`; `research_sources` is reused; `activity_logs` maps to sanitized existing audit records. No redundant tables are necessary for those concepts.

Original canonical BrandProfile 1.0 schemas and old API snapshots remain frozen. Additive BrandProfile 1.1 supports a null website for a social-only brand. A real presence URL is used as the supplied-input citation/crawl seed, never relabelled as an invented website.

## Running the integrated application locally

Use the existing README to provision the database, migration/API logins, signing secret and bound crawl/research service identities. Apply all migrations using `scripts/migrate.py`. The optional `web/` directory contains the same modular frontend as the private design review, excluding download archives and Site identity.

Set `MI_UI_DIRECTORY` to the absolute `web` directory and run the existing FastAPI application (for example `uvicorn app.main:app --host 127.0.0.1 --port 8000`). Open `/app/#brandbrain`. The UI calls `/v1/...` on the same origin and uses the existing login endpoints. No provider key belongs in browser configuration. Access and refresh tokens stay in memory, not browser storage. Re-authentication after refresh restores the backend draft, job and version state for the selected account/brand.

Run the existing bounded crawl worker separately. Public access obeys the existing robots/SSRF/size/time restrictions. Configure a research provider only on the server, with its existing usage/pricing limits, and run the existing bound research worker when a real pilot is authorized. No credentials, real model calls or public-source success are assumed by this release.

## Honest research sequence

1. The four-group setup creates/reuses a brand workspace using the existing create-brand endpoint. Workspace creation queues one real public-capture job. The adapter saves/submits a resumable session linked to that job; it does not queue a duplicate paid call.
2. The progress screen polls persisted status. Pending, failed, cancelled and needs-review stages remain visible. No ETA is invented. The completed-stage count is a count of actual completed stages, not a simulated percentage. Retry uses the existing workflow endpoint and worker.
3. After actual capture succeeds, synthesis deterministically organises observed metadata and founder inputs into six chapters. Unsupported audience, market or positioning remains an explicit gap. No LLM is needed to manufacture an attractive overview.
4. Broader research is an explicit action using the existing provider-gated research endpoint. Its legacy confirmed-intake gate can require targeted business/audience/budget inputs at this point; the initial four-group setup does not front-load them. The progress form confirms those inputs through the existing intake/profile APIs. Unknown critical inputs continue to block paid research; no existing safety gate is weakened.
5. Successfully validated dossier output can be explicitly imported into a new, unconfirmed Brand Brain review draft. Findings retain citations, confidence and AI/public provenance. This does not automatically approve the original research dossier for downstream strategy; Phase 3 semantic research approval remains required there.
6. Founder/admin review confirms individual findings. Edits/comments/replies/resolutions are persisted and attributed. Confirming selected complete chapters creates a partial snapshot; confirming all six creates a full snapshot. Unknown findings cannot be accepted as verified. Unresolved comments in selected chapters block confirmation.
7. `/brain/current` exposes the current confirmed structured Brain to other modules. Existing canonical-profile promotion is separate and retains its critical-gap and workflow checks; `/versions/{id}/canonical-profile` composes the existing save/submit/confirm APIs. A partial Brain never bypasses strategy prerequisites.

An existing confirmed Brain opens directly at its overview. A new draft or restored previous version never changes the current confirmed snapshot. A new public/paid research cycle is never started by viewing or editing. Requests to start/retry research obey the current existing workflow state, so unavailable lifecycle transitions return a clear conflict instead of silently replacing approved dependencies.

## API routes

All routes below are under `/v1/brands/{brand_id}/brain` and require the existing bearer session. Nested IDs always include brand scoping. Input errors are 422; authorization hides inaccessible brands with 404; missing/expired sessions return 401; stale revisions/invalid workflow gates return 409.

| Method / suffix | Purpose |
|---|---|
| POST `/sessions` | Create/reuse an incomplete draft for the authorized brand |
| GET `/sessions/current` | Resume the server draft/submission |
| PUT `/sessions/{id}` | Save typed partial inputs with expected_version |
| POST `/sessions/{id}/submit` | Validate all four groups and submit the linked real capture workflow |
| GET `/sessions/{id}/progress` | Actual job/stage state, recorded sources, limitations and recovery capabilities |
| POST `/sessions/{id}/synthesise` | Materialise chapters from captured evidence; idempotent by default; refresh=true explicitly creates a new draft |
| POST `/sessions/{id}/prepare-research` | Confirm targeted intake through existing profile APIs; no paid generation |
| POST `/sessions/{id}/research` | Existing ResearchStart contract, Idempotency-Key, provider/budget/intake gates |
| GET `/overview` | Latest draft, chapter status, quality warnings and confirmation/permission context |
| GET `/versions` | Bounded immutable version history |
| GET `/versions/{id}` | Read a previous version and its reviews |
| GET `/versions/{id}/chapters/{key}` | One reusable chapter contract |
| POST `/versions/{id}/findings/{finding}/review` | Accept/edit/reject/revise/mark unknown, with sequence and reason |
| POST `/versions/{id}/threads` | Comment on a chapter or finding |
| POST `/threads/{id}/comments` | Reply to a thread |
| POST `/threads/{id}/state` | Resolve/reopen with optimistic event matching |
| GET `/activity` | Sanitized brand-only actor/time/action history |
| POST `/versions/{id}/confirm` | Founder/admin partial/full confirmation with exact review sequence |
| GET `/current` | Latest confirmed structured snapshot, actor, timestamp and hash |
| POST `/versions/{id}/fork` | Restore/update into a new draft; preserve current confirmation |
| POST `/versions/{id}/canonical-profile` | Full Brain → existing canonical intake confirmation, preserving all gates |

Existing GET `/profile`, `/onboarding/questionnaire`, `/workflow`, POST `/crawls`, `/workflow/events`, research dossier/evidence/semantic-review APIs remain available; the adapter does not duplicate their implementation. Current OpenAPI: `openapi-brand-brain.json`. New JSON schemas are in `schemas/brain-*`, `brand-brain.schema.json`, and `brand-profile-v1.1.schema.json`.

## Security and data rules

- Seven new tables have enabled and forced RLS, composite brand/organization and parent/brand references, append-only originals/reviews/confirmation records and existing audit triggers.
- Only assigned human users with onboarding scopes can read/collaborate. Service identities cannot be human reviewers. Clients can comment/edit unconfirmed findings but cannot accept findings as founder-confirmed, confirm a Brain, synthesize raw internal research or access another brand.
- Final confirmations and modifications of founder-confirmed information require founder/admin authority. Current confirmed versions are immutable. A fork preserves original_text and the prior full version history.
- Draft, finding, thread and confirmation writes enforce expected versions/sequences/event IDs. Thread/comment changes and final review serialize under the brand lock.
- Provenance values: publicly_observed, ai_inferred, founder_provided, founder_confirmed, unverified. Confidence: high, medium, low, unknown. Chapter states: not_started, in_review, changes_requested, approved, needs_attention.
- Public observation means a source contains a statement; it is not independent truth. Founder confirmation is a human annotation and does not rewrite the original evidence.
- No cross-origin API token destination is accepted by the UI. Provider keys remain server-only. SQL uses parameter binding. Finding/comment/source/user strings are escaped before rendering.

## Limits requiring a real environment

A live public/provider run and browser rendering/keyboard/mobile/accessibility checks are not established by fixture tests. The hosted Sites preview has no backend route, worker or provider attached; sign-in therefore reports the unavailable API and does not claim submission succeeded. Production hosting, real account onboarding and the broader dashboard rollout remain deferred. Existing workflow lifecycle gates can block a new research cycle after later-stage approvals; the UI reports those conflicts and retains the current confirmed Brain rather than cancelling work or resetting budgets implicitly.
