# Phase 4 API and operations

Canonical contract: `openapi-phase4.json`. Prefix: `/v1/brands/{brand_id}/planning`. Bearer authentication, current-session validation and forced brand RLS apply. Clients cannot read these internal records. Founder/admin reviews are implemented here; assigned-client production approvals belong to Phase 5.

| Method | Route | Purpose |
|---|---|---|
| POST | `/templates/initialize` | Founder/admin initializes four immutable version 1 role templates for this brand. |
| GET | `/owners` | Internal staff obtains active assigned agency-human IDs; no emails are exposed. |
| POST | `/runs` | `planning:run` queues an explicitly selected stage with an Idempotency-Key. |
| GET | `/runs/{job_id}` | Internal status, latest deterministic validation, output ID and workflow-wide usage/reservations. |
| GET | `/artifacts?stage=strategy&limit=20` | Bounded internal list, 1–50 items; optional stage filter. |
| GET | `/artifacts/{output_id}` | Typed proposal, exact artifact ID/version/hash, parent references and immutable human review. |
| POST | `/artifacts/{output_id}/review` | Founder/admin approves, requests revision or rejects the current exact output. |

## Queueing

```json
{"stage":"strategy","expected_workflow_version":12}
```

Valid stages: `strategy`, `strategy_critique`, `thirty_day_plan`, `creative_brief`. Use 8–128 printable ASCII characters in the required Idempotency-Key. Repeating the same actor/key/body returns the original accepted job; changing the body under that key returns 409. Poll the run for current status.

Each stage uses server-selected current parents; callers cannot inject parent IDs, system prompts, sources or permissions. Strategy needs current confirmed profile plus Phase 3 human-semantic-approved research. Critique needs the current strategy. Plan needs exact human option/KPI selection. Creative needs the current human-approved plan.

Plan requests additionally require an explicit ISO `starts_on` date and may specify `owner_user_ids` from `/owners`; the current caller is the default owner. Dates/owners are rejected on other stages. The server captures immutable parent hashes, template version and selected human inputs in the job/dispatch. No external launch or calendar event is scheduled.

## Human review

All decisions need `expected_workflow_version`, `artifact_sha256`, `decision` and `reason`. Approval additionally requires `evidence_checked` and `dependencies_checked` true. These flags record a real human judgment; they are not prefilled model approval.

For strategy selection, supply:

- `distinctness_checked: true` and the selected **logical** `selected_option_id` from the proposal content.
- `critique_sha256` for the current matching red-team artifact.
- `approved_kpis`: all KPIs of that option with measurable targets. Names/definitions/units/measurement sources/review days cannot be silently changed; human target/baseline adjustments are recorded. Targets must be finite and ordered; percentage values must fit 0–100.
- `issue_acknowledgements`: one per selected-option critique issue, with zero-based `issue_index`, a human `rationale` and `accepted_risk: true`. This cannot override a blocking issue or a revise/reject critic verdict.

Plan approval needs `owners_and_schedule_checked` and `budget_checked`. Creative approval needs `compliance_checked`. All approvals revalidate the current stored output against current parent versions, allowed owners, approved claims and KPIs. Reviewing a stale hash, stale critique or previous version returns 409. A red-team output itself has no human-review endpoint: review the strategy it critiques.

Revisions and rejections are durable decisions, not deletions. They return to the appropriate generation queue without automatically creating a job. The next explicitly queued version receives the human reason as untrusted feedback; it cannot become a system instruction or waive evidence gates. Plans/creative outputs stay internal drafts until subsequent delivery approval services exist.

## Worker setup

Apply all migrations with the administrative runner. Provision a separate login with:

```sh
python scripts/provision.py runtime-role --name mi_planning_runtime --kind worker
python scripts/provision.py planning-service --organization-id UUID --brand-id UUID --database-role mi_planning_runtime
```

Use the returned service ID and dedicated worker database URL. Standalone workers use their own `MI_DATABASE_URL` and `MI_SERVICE_USER_ID`; they receive no API JWT signing secret. Run `python -m app.planning_worker`, optionally `--once` or `--stage strategy`. Compose supplies the optional `planning` profile. Initialize templates as founder/admin before delegating queueing to team members with `planning:run` and the necessary internal read scopes.

Planning requires server-only `MI_ALLOW_PLANNING_CALLS=true` in addition to existing provider/model/key and `MI_ALLOW_MODEL_CALLS=true`. The new flag defaults false. API admission and worker configuration must agree. No key is accepted in a request body or stored in source. Defaults: input limit 24,000 counted tokens and output limit 8,192. Oversized contexts pause instead of dropping evidence silently.

Shared workflow defaults remain 20 admitted model calls and 100,000 tokens across research, critique, plans, creative and revisions. Optional `MI_MODEL_MAX_COST_PAISE` is a dated configured INR estimate. If the legacy research cap is also set, the smaller cap wins. Add optional numeric rate/date/cap variables explicitly to the Compose services after configuring valid values; unset optional integers must be omitted, not passed as blank strings. Prices, taxes, FX and invoices are not fetched or certified.

## Failure and recovery

The existing atomic reservation, 900-second lease and maximum three attempts apply. Two invalid outputs pause for human review; known usage and raw output remain in append-only records. Worker loss after dispatch or an uncertain provider response holds reservations and never automatically repeats the generation. Founder/admin can request a new run with `acknowledge_uncertain_charge: true`; prior reservations remain held. A late known response may settle observed usage after stage progression, while its expired/cancelled output cannot advance the workflow.

Cancellation uses the existing `/workflow/events` route. Generic crawl retry is blocked for non-crawl jobs; resume through the corresponding research/planning run endpoint. Budget overruns are recorded honestly and stop further admitted calls. Client access, service self-approval, wrong-brand nested IDs, stale versions, blocking critique, invalid owners/KPIs and unmet stage gates are rejected.

No deployment, marketing account write, publishing, spending, client messaging or actual task execution occurs through these routes. The private dashboard preview remains sample data until Phase 5 API wiring.
