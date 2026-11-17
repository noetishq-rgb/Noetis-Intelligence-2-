BEGIN;
-- Immutable output envelopes bind approvals to exact versions.
CREATE TABLE mi.artifacts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 kind text NOT NULL CHECK(kind IN ('brand_profile','research_dossier','strategy_options','red_team_review','campaign_plan','creative_brief','recommendation','report','presentation')),
 version integer NOT NULL CHECK(version>0), logical_key uuid NOT NULL, schema_version text NOT NULL DEFAULT '1.0',
 created_by uuid NOT NULL REFERENCES identity.users(id), created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(id,brand_id), UNIQUE(id,brand_id,version), UNIQUE(brand_id,kind,logical_key,version),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id)
);
CREATE TABLE mi.workflow_instances (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 state text NOT NULL DEFAULT 'brand_created', version integer NOT NULL DEFAULT 1 CHECK(version>0), validation_failures integer NOT NULL DEFAULT 0 CHECK(validation_failures BETWEEN 0 AND 2), agent_calls integer NOT NULL DEFAULT 0 CHECK(agent_calls>=0), max_agent_calls integer NOT NULL DEFAULT 20 CHECK(max_agent_calls BETWEEN 1 AND 100), tokens_used integer NOT NULL DEFAULT 0 CHECK(tokens_used>=0), token_budget integer NOT NULL DEFAULT 100000 CHECK(token_budget>0), approved_profile_id uuid, approved_strategy_id uuid, last_error text, created_by uuid NOT NULL REFERENCES identity.users(id), CHECK(agent_calls<=max_agent_calls), CHECK(tokens_used<=token_budget), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(approved_profile_id,brand_id) REFERENCES mi.artifacts(id,brand_id),
 FOREIGN KEY(approved_strategy_id,brand_id) REFERENCES mi.artifacts(id,brand_id)
);
CREATE INDEX workflow_instances_tenant ON mi.workflow_instances(organization_id,brand_id);
CREATE UNIQUE INDEX one_active_workflow ON mi.workflow_instances(brand_id) WHERE state NOT IN ('completed','cancelled');
CREATE TABLE mi.workflow_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 workflow_id uuid NOT NULL, event text NOT NULL, from_state text NOT NULL, to_state text NOT NULL, workflow_version integer NOT NULL, actor_id uuid NOT NULL REFERENCES identity.users(id), artifact_id uuid, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(workflow_id,brand_id) REFERENCES mi.workflow_instances(id,brand_id),
 FOREIGN KEY(artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id), UNIQUE(workflow_id,workflow_version)
);
CREATE INDEX workflow_events_tenant ON mi.workflow_events(organization_id,brand_id);
CREATE TABLE mi.jobs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 workflow_id uuid NOT NULL, kind text NOT NULL CHECK(kind='website_crawl'), payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'), status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','retry','succeeded','failed','cancelled')), attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3), max_attempts integer NOT NULL DEFAULT 3 CHECK(max_attempts BETWEEN 1 AND 3), validation_failures integer NOT NULL DEFAULT 0 CHECK(validation_failures BETWEEN 0 AND 2), available_at timestamptz NOT NULL DEFAULT now(), lease_id uuid, lease_expires_at timestamptz, last_error text, created_by uuid NOT NULL REFERENCES identity.users(id), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(workflow_id,brand_id) REFERENCES mi.workflow_instances(id,brand_id)
);
CREATE INDEX jobs_tenant ON mi.jobs(organization_id,brand_id);
CREATE UNIQUE INDEX one_active_crawl ON mi.jobs(brand_id) WHERE status IN('queued','running','retry');
CREATE INDEX job_claim ON mi.jobs(available_at) WHERE status IN('queued','retry','running');
CREATE TABLE mi.idempotency_keys (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 actor_id uuid NOT NULL REFERENCES identity.users(id), operation text NOT NULL, key text NOT NULL CHECK(length(key) BETWEEN 8 AND 128), request_hash text NOT NULL CHECK(length(request_hash)=64), response jsonb, expires_at timestamptz NOT NULL DEFAULT now()+interval '24 hours', created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id), UNIQUE(brand_id,actor_id,operation,key)
);
CREATE INDEX idempotency_keys_tenant ON mi.idempotency_keys(organization_id,brand_id);
CREATE TABLE mi.brand_profiles (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 artifact_id uuid NOT NULL UNIQUE, version integer NOT NULL CHECK(version>0), profile jsonb NOT NULL CHECK(jsonb_typeof(profile)='object'), status text NOT NULL CHECK(status IN('draft','confirmed')), created_by uuid NOT NULL REFERENCES identity.users(id), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id), UNIQUE(brand_id,version)
);
CREATE INDEX brand_profiles_tenant ON mi.brand_profiles(organization_id,brand_id);
CREATE TABLE mi.onboarding_responses (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 respondent_id uuid NOT NULL REFERENCES identity.users(id), version integer NOT NULL CHECK(version>0), status text NOT NULL CHECK(status IN('draft','submitted')), answers jsonb NOT NULL CHECK(jsonb_typeof(answers)='object'), submitted_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id), UNIQUE(brand_id,respondent_id,version), CHECK((status='submitted')=(submitted_at IS NOT NULL))
);
CREATE INDEX onboarding_responses_tenant ON mi.onboarding_responses(organization_id,brand_id);
CREATE TABLE mi.research_sources (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 url text NOT NULL, publisher text, published_at date, accessed_at timestamptz NOT NULL, geographic_scope text NOT NULL CHECK(geographic_scope IN('india','global','unknown')), applicability_to_india text, access_limitations jsonb NOT NULL DEFAULT '[]', content_sha256 text CHECK(content_sha256 IS NULL OR length(content_sha256)=64), provenance text NOT NULL CHECK(provenance IN('website','client_input','public_web','social','review','search','upload')), CHECK(geographic_scope<>'global' OR applicability_to_india IS NOT NULL), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id)
);
CREATE INDEX research_sources_tenant ON mi.research_sources(organization_id,brand_id);
CREATE TABLE mi.crawl_pages (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 job_id uuid NOT NULL, source_id uuid NOT NULL, url text NOT NULL, http_status integer NOT NULL CHECK(http_status BETWEEN 100 AND 599), title text, description text, text_excerpt text NOT NULL, content_sha256 text NOT NULL CHECK(length(content_sha256)=64), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(job_id,brand_id) REFERENCES mi.jobs(id,brand_id),
 FOREIGN KEY(source_id,brand_id) REFERENCES mi.research_sources(id,brand_id)
);
CREATE INDEX crawl_pages_tenant ON mi.crawl_pages(organization_id,brand_id);
CREATE TABLE mi.research_dossiers (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 artifact_id uuid NOT NULL UNIQUE, profile_artifact_id uuid NOT NULL, body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id),
 FOREIGN KEY(profile_artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id)
);
CREATE INDEX research_dossiers_tenant ON mi.research_dossiers(organization_id,brand_id);
CREATE TABLE mi.competitor_profiles (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 dossier_id uuid NOT NULL, body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(dossier_id,brand_id) REFERENCES mi.research_dossiers(id,brand_id)
);
CREATE INDEX competitor_profiles_tenant ON mi.competitor_profiles(organization_id,brand_id);
CREATE TABLE mi.audience_segments (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 dossier_id uuid NOT NULL, body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(dossier_id,brand_id) REFERENCES mi.research_dossiers(id,brand_id)
);
CREATE INDEX audience_segments_tenant ON mi.audience_segments(organization_id,brand_id);
CREATE TABLE mi.strategy_options (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 artifact_id uuid NOT NULL, dossier_id uuid NOT NULL, option_key uuid NOT NULL, body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id),
 FOREIGN KEY(dossier_id,brand_id) REFERENCES mi.research_dossiers(id,brand_id), UNIQUE(artifact_id,option_key), UNIQUE(id,brand_id,artifact_id)
);
CREATE INDEX strategy_options_tenant ON mi.strategy_options(organization_id,brand_id);
CREATE TABLE mi.approvals (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 artifact_id uuid NOT NULL, artifact_version integer NOT NULL, assigned_to uuid NOT NULL REFERENCES identity.users(id), decided_by uuid REFERENCES identity.users(id), decision text NOT NULL CHECK(decision IN('pending','approved','revision_requested','rejected')), reason text, decided_at timestamptz, CHECK((decision='pending')=(decided_at IS NULL)), CHECK((decision='pending')=(decided_by IS NULL)), CHECK(decided_by IS NULL OR assigned_to=decided_by), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id), FOREIGN KEY(artifact_id,brand_id,artifact_version) REFERENCES mi.artifacts(id,brand_id,version)
);
CREATE INDEX approvals_tenant ON mi.approvals(organization_id,brand_id);
CREATE UNIQUE INDEX one_pending_approval ON mi.approvals(artifact_id,assigned_to) WHERE decision='pending';
CREATE TABLE mi.strategy_approvals (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 approval_id uuid NOT NULL, selected_option_id uuid NOT NULL, strategy_artifact_id uuid NOT NULL, approved_kpis jsonb NOT NULL CHECK(jsonb_typeof(approved_kpis)='array'), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(approval_id,brand_id) REFERENCES mi.approvals(id,brand_id),
 FOREIGN KEY(strategy_artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id), FOREIGN KEY(selected_option_id,brand_id,strategy_artifact_id) REFERENCES mi.strategy_options(id,brand_id,artifact_id)
);
CREATE INDEX strategy_approvals_tenant ON mi.strategy_approvals(organization_id,brand_id);
CREATE TABLE mi.red_team_reviews (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 artifact_id uuid NOT NULL UNIQUE, strategy_artifact_id uuid NOT NULL, body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id),
 FOREIGN KEY(strategy_artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id)
);
CREATE INDEX red_team_reviews_tenant ON mi.red_team_reviews(organization_id,brand_id);
CREATE TABLE mi.campaign_plans (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 artifact_id uuid NOT NULL UNIQUE, strategy_approval_id uuid NOT NULL, starts_on date NOT NULL, duration_days integer NOT NULL DEFAULT 30 CHECK(duration_days=30), body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id),
 FOREIGN KEY(strategy_approval_id,brand_id) REFERENCES mi.strategy_approvals(id,brand_id)
);
CREATE INDEX campaign_plans_tenant ON mi.campaign_plans(organization_id,brand_id);
CREATE TABLE mi.calendar_tasks (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 plan_id uuid NOT NULL, day integer NOT NULL CHECK(day BETWEEN 1 AND 30), owner_id uuid NOT NULL REFERENCES identity.users(id), deliverable text NOT NULL, success_metric jsonb NOT NULL, approval_required boolean NOT NULL DEFAULT true, body jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(plan_id,brand_id) REFERENCES mi.campaign_plans(id,brand_id)
);
CREATE INDEX calendar_tasks_tenant ON mi.calendar_tasks(organization_id,brand_id);
CREATE TABLE mi.task_dependencies (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 task_id uuid NOT NULL, dependency_id uuid NOT NULL, CHECK(task_id<>dependency_id), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(task_id,brand_id) REFERENCES mi.calendar_tasks(id,brand_id),
 FOREIGN KEY(dependency_id,brand_id) REFERENCES mi.calendar_tasks(id,brand_id), UNIQUE(task_id,dependency_id)
);
CREATE INDEX task_dependencies_tenant ON mi.task_dependencies(organization_id,brand_id);
CREATE TABLE mi.creative_briefs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 artifact_id uuid NOT NULL UNIQUE, plan_id uuid NOT NULL, task_id uuid NOT NULL, body jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id),
 FOREIGN KEY(plan_id,brand_id) REFERENCES mi.campaign_plans(id,brand_id),
 FOREIGN KEY(task_id,brand_id) REFERENCES mi.calendar_tasks(id,brand_id)
);
CREATE INDEX creative_briefs_tenant ON mi.creative_briefs(organization_id,brand_id);
CREATE TABLE mi.creative_assets (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 brief_id uuid, object_key text NOT NULL, content_type text NOT NULL, byte_size bigint NOT NULL CHECK(byte_size BETWEEN 0 AND 52428800), sha256 text NOT NULL CHECK(length(sha256)=64), scan_status text NOT NULL DEFAULT 'pending' CHECK(scan_status IN('pending','clean','rejected')), created_by uuid NOT NULL REFERENCES identity.users(id), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(brief_id,brand_id) REFERENCES mi.creative_briefs(id,brand_id)
);
CREATE INDEX creative_assets_tenant ON mi.creative_assets(organization_id,brand_id);
CREATE TABLE mi.prompt_templates (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 task_type text NOT NULL, version integer NOT NULL CHECK(version>0), context_fields jsonb NOT NULL CHECK(jsonb_typeof(context_fields)='array'), template text NOT NULL, output_schema text NOT NULL, max_input_tokens integer NOT NULL CHECK(max_input_tokens>0), max_output_tokens integer NOT NULL CHECK(max_output_tokens>0), created_by uuid NOT NULL REFERENCES identity.users(id), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id), UNIQUE(brand_id,task_type,version)
);
CREATE INDEX prompt_templates_tenant ON mi.prompt_templates(organization_id,brand_id);
CREATE TABLE mi.prompt_runs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 workflow_id uuid NOT NULL, template_id uuid NOT NULL, body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'), input_tokens integer NOT NULL DEFAULT 0 CHECK(input_tokens>=0), output_tokens integer NOT NULL DEFAULT 0 CHECK(output_tokens>=0), cost_paise bigint CHECK(cost_paise>=0), currency text NOT NULL DEFAULT 'INR' CHECK(currency='INR'), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(workflow_id,brand_id) REFERENCES mi.workflow_instances(id,brand_id),
 FOREIGN KEY(template_id,brand_id) REFERENCES mi.prompt_templates(id,brand_id)
);
CREATE INDEX prompt_runs_tenant ON mi.prompt_runs(organization_id,brand_id);
CREATE TABLE mi.agent_runs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 workflow_id uuid NOT NULL, prompt_run_id uuid, agent_role text NOT NULL, status text NOT NULL CHECK(status IN('queued','running','succeeded','failed','needs_human_review')), attempt integer NOT NULL CHECK(attempt BETWEEN 1 AND 3), validation_failures integer NOT NULL DEFAULT 0 CHECK(validation_failures BETWEEN 0 AND 2), latency_ms integer CHECK(latency_ms>=0), error_code text, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(workflow_id,brand_id) REFERENCES mi.workflow_instances(id,brand_id),
 FOREIGN KEY(prompt_run_id,brand_id) REFERENCES mi.prompt_runs(id,brand_id)
);
CREATE INDEX agent_runs_tenant ON mi.agent_runs(organization_id,brand_id);
CREATE TABLE mi.agent_outputs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 agent_run_id uuid NOT NULL, artifact_id uuid, output_schema text NOT NULL, valid boolean NOT NULL, body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(agent_run_id,brand_id) REFERENCES mi.agent_runs(id,brand_id),
 FOREIGN KEY(artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id)
);
CREATE INDEX agent_outputs_tenant ON mi.agent_outputs(organization_id,brand_id);
CREATE TABLE mi.integrations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 provider text NOT NULL CHECK(provider IN('meta','instagram','facebook','linkedin','youtube','gsc','ga4','email','crm','whatsapp')), status text NOT NULL DEFAULT 'disabled' CHECK(status IN('disabled','pending','connected','expired','revoked','error')), scopes text[] NOT NULL DEFAULT '{}', access_mode text NOT NULL DEFAULT 'read' CHECK(access_mode IN('read','write')), external_account_id text, last_sync_at timestamptz, last_error text, created_by uuid NOT NULL REFERENCES identity.users(id), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id), UNIQUE(brand_id,provider,external_account_id)
);
CREATE INDEX integrations_tenant ON mi.integrations(organization_id,brand_id);
CREATE UNIQUE INDEX integration_brand_key ON mi.integrations(id,brand_id,organization_id);
CREATE TABLE mi.integration_credentials (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 integration_id uuid NOT NULL, ciphertext bytea NOT NULL, nonce bytea NOT NULL CHECK(octet_length(nonce)=12), wrapped_key bytea NOT NULL, kms_key_id text NOT NULL, algorithm text NOT NULL DEFAULT 'AES-256-GCM' CHECK(algorithm='AES-256-GCM'), aad text NOT NULL, key_version integer NOT NULL CHECK(key_version>0), expires_at timestamptz, revoked_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id), FOREIGN KEY(integration_id,brand_id,organization_id) REFERENCES mi.integrations(id,brand_id,organization_id)
);
CREATE INDEX integration_credentials_tenant ON mi.integration_credentials(organization_id,brand_id);
CREATE TABLE mi.performance_snapshots (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 integration_id uuid NOT NULL, external_entity_id text NOT NULL, entity_type text NOT NULL CHECK(entity_type IN('account','campaign','ad_set','ad','creative','audience','page','query','email','lead')), period_start date NOT NULL, period_end date NOT NULL CHECK(period_end>=period_start), breakdown_key text NOT NULL, attribution_settings jsonb NOT NULL, reporting_timezone text NOT NULL, currency text NOT NULL DEFAULT 'INR' CHECK(currency='INR'), metrics jsonb NOT NULL, source_as_of timestamptz NOT NULL, is_partial boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(integration_id,brand_id) REFERENCES mi.integrations(id,brand_id), UNIQUE(integration_id,external_entity_id,period_start,period_end,breakdown_key)
);
CREATE INDEX performance_snapshots_tenant ON mi.performance_snapshots(organization_id,brand_id);
CREATE TABLE mi.recommendations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 artifact_id uuid NOT NULL UNIQUE, snapshot_id uuid, body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'), approval_required boolean NOT NULL DEFAULT true CHECK(approval_required), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id),
 FOREIGN KEY(snapshot_id,brand_id) REFERENCES mi.performance_snapshots(id,brand_id)
);
CREATE INDEX recommendations_tenant ON mi.recommendations(organization_id,brand_id);
CREATE TABLE mi.reports (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 artifact_id uuid NOT NULL UNIQUE, audience text NOT NULL CHECK(audience IN('internal','client')), language text NOT NULL DEFAULT 'en', body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'), object_key text, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id)
);
CREATE INDEX reports_tenant ON mi.reports(organization_id,brand_id);
CREATE TABLE mi.presentations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 artifact_id uuid NOT NULL UNIQUE, audience text NOT NULL CHECK(audience IN('internal','client')), language text NOT NULL DEFAULT 'en', body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'), object_key text, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id)
);
CREATE INDEX presentations_tenant ON mi.presentations(organization_id,brand_id);
CREATE TABLE mi.published_deliverables (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 artifact_id uuid NOT NULL, approval_id uuid NOT NULL, kind text NOT NULL, projection_schema text NOT NULL, client_payload jsonb NOT NULL CHECK(jsonb_typeof(client_payload)='object'), published_by uuid NOT NULL REFERENCES identity.users(id), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id),
 FOREIGN KEY(approval_id,brand_id) REFERENCES mi.approvals(id,brand_id), UNIQUE(artifact_id)
);
CREATE INDEX published_deliverables_tenant ON mi.published_deliverables(organization_id,brand_id);
CREATE TABLE mi.notifications (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 recipient_id uuid NOT NULL REFERENCES identity.users(id), kind text NOT NULL, message text NOT NULL, read_at timestamptz, external_delivery boolean NOT NULL DEFAULT false CHECK(NOT external_delivery), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id)
);
CREATE INDEX notifications_tenant ON mi.notifications(organization_id,brand_id);
CREATE TABLE mi.audit_logs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL, brand_id uuid NOT NULL,
 actor_id uuid NOT NULL REFERENCES identity.users(id), action text NOT NULL, entity_type text NOT NULL, entity_id uuid, artifact_version integer, source_ids uuid[] NOT NULL DEFAULT '{}', input_hash text, output_hash text, error_code text, attempt integer NOT NULL DEFAULT 1 CHECK(attempt>0), occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(), previous_hash text, entry_hash text NOT NULL DEFAULT '', created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id)
);
CREATE INDEX audit_logs_tenant ON mi.audit_logs(organization_id,brand_id);
COMMIT;
