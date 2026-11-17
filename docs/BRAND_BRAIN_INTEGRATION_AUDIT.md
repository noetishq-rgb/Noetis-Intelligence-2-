# Brand Brain integration audit — 17 November 2026

Completed before implementation. This scope is the six-screen Brand Brain journey; the wider Phase 5/6 dashboard rollout remains deferred.

## Existing architecture and capabilities

- Frontend: buildless native JavaScript modules, hash routing, per-brand in-memory sample state, CSS tokens and a single shared sidebar/layout. Reusable badges, panels, chart helpers, native dialog/drawer, notifications and HTML/PPTX exporters. There is no React/component framework or installed UI dependency. Current hosted Sites publication is owner-only and static. It has no authenticated application API session or database connection; Sites owner access is separate from backend identity.
- Backend: FastAPI/Pydantic, psycopg explicit transactions, PostgreSQL 17; 25 applied migrations. Existing JWT/Argon2 login, rotation/revocation, founder/admin/team/client roles, per-brand scopes, forced RLS, composite tenant FKs, immutable artifacts, confirmations and chained audit logs.
- Brand Brain foundation: existing brand/profile/intake endpoints and website crawl jobs; extracted observations preserve source IDs. Research jobs capture bounded public pages, optionally discover sources, call one configured provider, validate provenance/quotation/coverage, require human semantic review and enforce cost/lease/retry limits. Phase 4 consumes only approved dependencies. No dedicated Brand Brain screens exist.
- Jobs: PostgreSQL durable leased `jobs`, source snapshots, tool traces and model dispatch accounting. No Redis, cron or realtime broker is needed; bounded polling fits the current system.
- Storage: PostgreSQL JSON/source excerpts plus creative-asset metadata. No configured object-store upload workflow. Internal notification rows exist; no external sends. Audit rows exist; no comments, reply threads or finding edit history exist.
- Brand/user association: organization memberships and brand memberships. Admins create brands; team access is scoped; clients can supply assigned intake and read published material. Service identities cannot act as human reviewers. A static frontend projection is not an authorization boundary.

## Reuse and necessary additions

Reuse `brand_workspaces`, `brand_profiles`, `onboarding_responses`, `jobs`, `crawl_pages`, `research_sources`, `research_evidence`, `research_dossiers`, `artifacts`, existing research/approval workflows and `audit_logs`. Do not create second research_jobs/research_sources/audit tables. Stages are a truthful API projection of persisted jobs/evidence, not a second queue. Sources/findings are structured into one six-chapter model with immutable originals.

Add only workspace onboarding sessions, versioned Brand Brain documents, append-only finding reviews, comment threads/messages/events, and confirmations. Online links and objectives are typed session/document fields; chapters/findings are structured version content. No redundant lookup tables are necessary. All new brand-owned rows have organization_id, brand_id, tenant FKs and forced RLS. Existing audit triggers record meaningful writes. Actor names and timestamps are stored with changes.

Extend existing create-brand inputs for category, objectives and multiple public presence links; support social-only presence without inventing a website. Preserve historical schema/API snapshots and append migrations. New `/v1/brands/{id}/brain` endpoints are an adapter over existing services for draft resume, status/sources, synthesis, chapter review, collaboration, version history and confirmation. Existing crawl/research starts/retries remain authoritative; do not weaken provider, semantic-review, budget or canonical-profile gates merely to animate onboarding.

## Integration approach, risks and limits

Mount a scoped dark Brand Brain experience in the existing shell and add one navigation entry. Reuse existing login APIs; tokens stay in memory. Persist authenticated drafts and reviews on the backend, and only the four initial inputs as a recoverable local draft when disconnected. Backend API origin is deployment configuration, never a field that collects provider secrets. The static private review must say when it is disconnected and cannot claim to have launched research.

Start with real public capture from the four inputs. Show unavailable audience/market findings as gaps; reuse validated research dossiers when available. Existing paid broad research needs a confirmed intake/provider and an explicit start: show these prerequisites instead of inventing stages or silently spending. Derive stage state from actual records. No unreliable ETA or fabricated percent. Founder-confirmed versions remain current until explicit replacement. Partial confirmation records verified sections and leaves missing sections visible; downstream strategy readiness remains governed by the existing canonical-profile/research gates.

The API is not deployed; provider credentials and authentic intake remain absent. Real paid-model quality, public-source reachability and browser visual/accessibility QA cannot be established from fixture tests. These are explicit deployment/pilot prerequisites, not permission reasons to leave local implementation unfinished.

## Incremental implementation and checks

1. Audit (this document): map contracts, tenancy and reuse before code.
2. Brand setup: four groups, URL detection/validation, objectives, recoverable drafts, authenticated session and existing workspace/crawl submission.
3. Progress: poll actual capture/research records, source inspection, truthful pending/input/failure states and existing retry routes; resume server state after login/refresh.
4. Intelligence: reusable six-chapter documents, provenance/confidence, real source metadata, immutable originals and optimistic finding reviews.
5. Collaboration/confirmation: tenant-scoped threads/replies/resolution, immutable activity/version history, conflict checks, partial/full confirmation and canonical-profile promotion only when its existing gates are satisfied.
6. Verify migration replay, API auth/tenant restrictions, draft/real-job/synthesis/review/comment/version paths and existing dashboard/backend regression suites; package updated private UI and source. No other module redesign or production rollout.
