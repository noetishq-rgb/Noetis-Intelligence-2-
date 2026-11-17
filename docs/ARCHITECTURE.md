# Architecture and implementation decisions

## Scope and stack

Python 3.12, FastAPI, Pydantic v2, psycopg 3 and PostgreSQL 17. PostgreSQL also provides a durable SKIP LOCKED job queue without a separate Redis installation. Phase 2 implements a provider-neutral model protocol with an optional Claude adapter. No frontend, OAuth account integration, file upload implementation or external execution is built.

The existing static dashboard prototype is separate and unchanged. The supplied project brief supersedes the earlier integration priorities: Meta, Search Console, GA4, WhatsApp and other private account integrations are later product-version work. Phase 1 fetches bounded public website HTML; Phase 2 adds bounded public research, optional search discovery and optional model calls.

## Components

- app/main.py: authenticated API, organization/brand creation, invitations, onboarding, confirmation, job/status and audit reads.
- app/models.py: canonical strict output models and onboarding input model.
- app/api_types.py: HTTP request/response projections.
- app/auth.py: password hashing, sessions, token rotation and permission checks.
- app/db.py: transaction-local identity and runtime privilege validation.
- app/repository.py: immutable profile persistence, idempotency hashing and optimistic workflow transitions.
- app/worker.py: scoped service principal, durable claims, fencing, retries and crawl result persistence.
- app/services/crawl.py: public network policy, pinned connections, robots and bounded extraction.
- app/services/extraction.py: evidence-based metadata extraction, explicit gaps and questionnaire generation.
- app/services/validation.py: JSON Schema plus semantic output validation and tenant/source-reference checks.
- app/research_api.py and research_worker.py: queue, evidence, research generation, usage accounting and human review.
- app/research_models.py: strict dossier 1.1, source/run/review and prompt-preview contracts.
- app/services/model_provider.py and anthropic_provider.py: provider-neutral protocol and optional Claude HTTP adapter.
- app/services/research_*.py: source collection, compact context, strict citation validation, immutable storage and budget settlement.

## Entity map

All requested minimum entities exist in the migrations:

| Domain | Tables |
|---|---|
| Identity | identity.users, organizations, organization_memberships, brand_memberships, identity.sessions, identity.invites |
| Intake | brand_workspaces, brand_profiles, onboarding_responses |
| Research | research_dossiers, research_sources, research_evidence, research_reviews, competitor_profiles, audience_segments, crawl_pages |
| Strategy | strategy_options, red_team_reviews, strategy_approvals, approvals |
| Planning | campaign_plans, calendar_tasks, task_dependencies |
| Creative | creative_briefs, creative_assets |
| Agent control | prompt_templates, prompt_runs, agent_runs, agent_outputs, model_dispatches |
| Workflow and observability | workflow_instances, workflow_events, jobs, tool_calls, audit_logs, idempotency_keys, identity.auth_events, identity.auth_attempts, service_principals |
| Future integrations | integrations, integration_credentials, performance_snapshots, recommendations |
| Delivery | reports, presentations, published_deliverables, notifications |
| Version identity | artifacts |

Unless prefixed identity., these tables are in schema mi. Brand workspaces expose both id and a generated brand_id alias. Every client-owned row includes brand_id and organization_id. Users/authentication state and organization control records are global or agency-owned exceptions.

## Versioning and approvals

Artifacts carry a logical key, version, schema version and content SHA256. Typed child records hold validated structured JSON. Brand profile versions, submitted and draft onboarding versions, research, prompts and agent outputs are append-only. SQL foreign keys bind relationships to the same brand. Client edits create new versions; they do not rewrite prior evidence.

Approvals target an exact artifact ID, version and hash, with an assigned human, workflow and requester. A pending request can transition once to a human decision; the finalized record is immutable. Strategy approval additionally binds a selected option and approved KPI set. Changed inputs invalidate downstream applicability: Phase 2 compares the current confirmed profile, latest dossier and exact hash at human research review. Later stages must apply the same dependency checks. Historical approvals remain historical records.

All money is INR using integer paise for budgets, costs and ranges. Reporting models retain attribution and source cutoffs. Language codes support English, Hindi and regional languages without assuming an English-only audience. Research citations explicitly label India, global or unknown scope.

## Background flow and recovery

Brand creation and its initial queued crawl are one database transaction. A worker authenticates to PostgreSQL with a separate restricted login and a provisioned service user ID. It can claim only assigned brands. Workflow rows lock before jobs; claims use SKIP LOCKED. A job gets a random lease ID and 120-second expiry. It performs no network I/O while holding a database transaction.

Completion locks the profile/workflow/job and checks the same unexpired lease. Stale workers cannot commit after cancellation or lease reassignment. Save sources, pages, profile, traces, job status and workflow transition in one transaction. Failed network work retries with bounded backoff; expired leases are reclaimed and exhausted attempts become failed. Two schema validation failures pause for human review. Progress remains stored across browser or worker restarts.

Research uses a separate database-bound worker principal and 900-second lease. Phase 2 counts input tokens, atomically reserves call/token/cost headroom, records dispatch intent and settles known usage. Unknown post-dispatch results pause without an automatic repeat. Default workflow allowance is 20 admitted calls and 100,000 tokens; optional dated INR rates support an estimated cost cap. See RESEARCH_OPERATIONS.md for accounting limits and recovery.

## Schema and semantic validation

The nine original output schemas, onboarding and Phase 2 research request/review/dossier 1.1 schemas are generated from Pydantic. Objects reject unknown fields. JSON Schema validates primitive types and formats; Pydantic adds semantics such as valid allocation sums, ordered monetary ranges, valid source references, exact review coverage, material risk counts, acyclic 30-day task dependencies and assigned human decisions. These checks are complementary: JSON Schema alone cannot establish that evidence is true or an idea is strategically distinct.

The extractor treats exact website metadata as an observation about what a site says, not independent verification of its claim. It records missing pricing, margins, ICP and performance rather than inventing them. A founder must confirm required intake inputs before research can start.

## Delivery status

Implemented and locally tested: foundation, authentication, tenant controls, intake/crawl and confirmed profile, plus public research adapters, compact prompt compilation, optional Claude integration, cited dossier validation, durable reservations/recovery and founder/admin research review. Designed but not implemented: strategy/critique/planning/creative generation, client approval portal, file export workers, uploads, private account integrations and external audit archive. Tests use recorded sources/provider responses; no live API quality or production deployment is claimed.

## Phase 3 validation build

Validator 3.0 enforces every-citation support and evidence-backed coverage, with durable diagnostics stored alongside immutable agent outputs. New reviews require explicit human semantic assessments and fresh deterministic revalidation; migration 018 adds a minimum database review guard without modifying old decisions. API 0.3.0 and its new review/report schemas are exported in openapi-phase3.json. See RESEARCH_VALIDATION.md and TEST_RESULTS_PHASE3.md. SuperYou is the selected pilot; live source capture and provider execution remain pending the prerequisites in SUPERYOU_PILOT.md.

## Phase 4 planning backend

API 0.4.0 adds planning_api.py, planning_worker.py, planning_models.py and services/planning.py. Durable planning_outputs and planning_reviews bind typed artifacts to dispatches, parent hashes and human decisions. Normalized strategy_options/red_team_reviews/strategy_approvals/campaign_plans/calendar_tasks/dependencies/creative_briefs are also populated. The shared model journal, budgets, fencing and uncertain-charge policy are reused. New strategy/plan 1.1 and creative-batch contracts preserve old snapshots; the current API is openapi-phase4.json. See PHASE_4.md and PLANNING_API.md.
