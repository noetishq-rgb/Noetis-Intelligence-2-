# Research operations and security

## Setup

Apply migrations 012–017 with the administrative migration runner. Existing Phase 1 migrations are unchanged. For pre-existing brands, have founder/admin call `POST /research/templates/initialize` before delegating research to team members.

Provision a separate database login with `python scripts/provision.py runtime-role --name mi_research_runtime --kind worker`, then `python scripts/provision.py research-service --organization-id UUID --brand-id UUID --database-role mi_research_runtime`. These are administrative commands; the service identity is bound to that login by `mi.service_principals`. Its scopes include research execution and required profile/job/workflow reads, but no human approvals or authentication access. Do not share this login with a crawl worker or unrelated brands.

Set the research worker's `MI_DATABASE_URL` to its dedicated login, and `MI_SERVICE_USER_ID` to the returned UUID. API/test orchestration can instead use `MI_WORKER_DATABASE_URL` to select the worker connection. Standalone workers need no JWT signing key. Run `python -m app.research_worker` or add `--once` for one claim/recovery pass. Provision identities before starting Compose workers.

For real model execution set server-only `MI_ALLOW_MODEL_CALLS=true`, `MI_MODEL_PROVIDER=anthropic`, a valid `MI_ANTHROPIC_API_KEY`, your accessible `MI_ANTHROPIC_MODEL`, and an API version supported by that model/account. Both the API admission check and research worker must receive matching provider configuration. The default version is 2026-10-21. Optional search needs `MI_ALLOW_SEARCH_CALLS=true` and `MI_BRAVE_API_KEY` on the worker. Manual eligible source URLs need no search API. Never put credentials in brand data, prompts, logs, repository files or the client browser. Only use keys supplied through environment/secret management; key-vault UI and tenant-specific key management are not implemented.

## Budgets and pricing

Each workflow defaults to at most 20 admitted generation calls and 100,000 tokens across research attempts. Per-call configured limits default to 16,000 counted input tokens and 4,096 output tokens. The worker counts the exact prompt/schema with the provider, adds input headroom of max(256, 10%), and reserves that plus the maximum output before dispatch under a workflow row lock. Counting calls do not consume the generation-call counter; source search requests have their own small per-job bounds.

Configure dated integer `MI_MODEL_INPUT_PAISE_PER_MILLION`, `MI_MODEL_OUTPUT_PAISE_PER_MILLION` and `MI_PRICING_AS_OF` to estimate INR model costs. These are deployment-supplied converted rates, not fetched prices or invoice totals. Optional `MI_RESEARCH_MAX_COST_PAISE` refuses dispatch unless pricing is complete and the workflow estimate plus reservation fits. Set rates/date/cap on every research worker; Compose intentionally omits unset optional integer fields. Search-provider charges, taxes, exchange changes and future special token rates are excluded. Cached token counts are retained in input usage, but the simple rate model is not a provider billing reconciliation system.

Observed usage is settled once with integer ceiling rounding. Provider overshoot is recorded even beyond a budget and pauses for human review; usage is never truncated to make the cap appear satisfied. `pricing_complete` is false if any admitted call lacked pricing. Unknown outcomes retain their token/cost reservations and nullable usage; they are not treated as zero usage. Budget caps should leave headroom for count estimates and billing differences.

## Failures, fencing and recovery

Claims lock workflow then job, use SKIP LOCKED and have a random 900-second research lease (crawl remains 120 seconds). At most three job attempts are allowed. Network work runs outside database transactions. No generation SDK/HTTP retries are enabled. Transient pre-generation/count failures may retry with bounded backoff. A definite rejected generation can release reservation; transport loss or server error after dispatch is treated as possibly charged and pauses.

Durable `model_dispatches` records the immutable exact request, template/profile references, hash and lease before sending. An expired reservation that was never marked dispatched is safely released. A lost dispatched request becomes `outcome_unknown`, retains reservation and stops. Founder/admin must explicitly acknowledge uncertain charge to request an additional run with a fresh key. There is no autonomous provider-billing reconciliation job; operations must verify unresolved charges with the provider. If a still-running worker eventually receives a response, it can settle known usage once while discarding output from a lost/cancelled lease. Human readiness is not restored automatically.

Two structurally/reference-invalid model outputs pause in `needs_human_review`. No accessible source pauses in `awaiting_inputs` before generation. Raw observed responses and costs survive cancellation; late output cannot create a new dossier. Revisions create new immutable versions. Repeated review submissions return a state conflict instead of overwriting decisions.

## Tenant isolation and evidence limits

Research sources/evidence, dossiers, templates, dispatches, prompt/agent outputs and reviews use forced brand RLS and same-brand composite foreign keys. Staff need assigned scopes; client roles cannot read internal research or costs. The actor is server-established from the session, and worker actor identity is database-bound. Only founder/admin can review and approve; a service/model cannot self-approve. Every review binds exact current artifact/hash and approved profile. Stored mutation and tool audit entries retain IDs/hashes and sanitized metadata.

Public evidence is fetched with the existing robots-aware, pinned-IP SSRF policy; provider URLs are fixed HTTPS hosts, no redirects or proxy environment inheritance. Source pages are capped at eight accepted snapshots / twelve candidates, one page per seed, 12,000 extracted characters retained and 2,400 supplied per source. Collection has no JavaScript or login access. Discovered snippets are not evidence. Citation publication date stays null if not reliably known. Hashes identify captured content, not continued availability or truth.

The model sees only task-allowlisted compact brand fields and selected excerpts. Private budget/margin notes, assets and full histories are excluded. All source content is untrusted data; the only model tool serializes the output schema. Strict validation checks references/quotes, not semantic truth. Founder review must inspect meaning, geographic applicability, stale content and sampling bias. No advertising, publishing, messaging, external file distribution or private account integration is enabled.

## Phase 3 upgrade

Apply migration 018 before API 0.3.0. For existing brands, initialize immutable template version 2 via `/research/templates/initialize`; prior templates/dispatches remain pinned. Follow RESEARCH_VALIDATION.md for new approval requirements and current diagnostics. `python scripts/research_pilot.py preflight` reports safe provider configuration flags without making network/model calls. The SuperYou capture manifest and actual blocked pilot status are documented in SUPERYOU_PILOT.md.
