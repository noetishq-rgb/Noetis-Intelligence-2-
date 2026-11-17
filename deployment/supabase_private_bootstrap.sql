-- NOETIS private application namespace. Reviewed fresh-install bundle; original migrations remain immutable.
DO $bootstrap$ DECLARE r text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=current_user AND rolcreaterole AND rolbypassrls) THEN RAISE EXCEPTION 'Administrative migration owner required'; END IF;
 FOREACH r IN ARRAY ARRAY['mi_app','mi_guard','mi_api'] LOOP
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=r AND (rolcanlogin OR rolsuper OR rolbypassrls)) THEN RAISE EXCEPTION 'Incompatible existing role %',r; END IF;
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=r) THEN EXECUTE format('CREATE ROLE %I NOLOGIN NOBYPASSRLS',r); END IF;
  EXECUTE format('GRANT %I TO %I',r,current_user);
 END LOOP;
 GRANT mi_app TO mi_api;
END $bootstrap$;
CREATE SCHEMA mi_meta;
REVOKE ALL ON SCHEMA mi_meta FROM PUBLIC;
CREATE TABLE mi_meta.schema_migrations(name text PRIMARY KEY,sha256 text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now(),profile text NOT NULL DEFAULT 'native',effective_sha256 text);
ALTER TABLE mi_meta.schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE mi_meta.schema_migrations FORCE ROW LEVEL SECURITY;
REVOKE ALL ON mi_meta.schema_migrations FROM PUBLIC;

-- Source: 001_identity_tenancy.sql

CREATE EXTENSION IF NOT EXISTS pgcrypto;
DO $$ BEGIN
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='mi_app') THEN CREATE ROLE mi_app NOLOGIN NOBYPASSRLS; END IF;
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='mi_guard') THEN CREATE ROLE mi_guard NOLOGIN BYPASSRLS; END IF;
END $$;
CREATE SCHEMA identity;
CREATE SCHEMA mi;
-- Existing public schema privileges are preserved by the managed profile.
REVOKE ALL ON SCHEMA identity,mi FROM PUBLIC;
GRANT USAGE ON SCHEMA mi,identity TO mi_app,mi_guard;
GRANT CREATE ON SCHEMA mi TO mi_guard;
CREATE FUNCTION mi.actor_id() RETURNS uuid LANGUAGE sql STABLE AS $$
 SELECT nullif(current_setting('app.actor_id',true),'')::uuid $$;
CREATE TABLE identity.users (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), email text NOT NULL UNIQUE CHECK(email=lower(email)),
 full_name text NOT NULL, password_hash text, disabled boolean NOT NULL DEFAULT false,
 is_service boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now(),
 CHECK ((is_service AND password_hash IS NULL) OR (NOT is_service AND password_hash IS NOT NULL))
);
CREATE TABLE identity.sessions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES identity.users(id),
 refresh_hash text NOT NULL UNIQUE, family_id uuid NOT NULL, expires_at timestamptz NOT NULL,
 consumed_at timestamptz, revoked_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_family ON identity.sessions(family_id);
CREATE TABLE identity.auth_attempts (
 bucket text PRIMARY KEY, failures integer NOT NULL DEFAULT 0 CHECK(failures>=0),
 window_start timestamptz NOT NULL DEFAULT now(), blocked_until timestamptz
);
CREATE TABLE mi.organizations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text NOT NULL,
 owner_id uuid NOT NULL REFERENCES identity.users(id), currency text NOT NULL DEFAULT 'INR' CHECK(currency='INR'),
 locale text NOT NULL DEFAULT 'en', timezone text NOT NULL DEFAULT 'Asia/Kolkata',
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE mi.organization_memberships (
 organization_id uuid NOT NULL REFERENCES mi.organizations(id), user_id uuid NOT NULL REFERENCES identity.users(id),
 role text NOT NULL CHECK(role IN ('founder','admin','team','client','service')),
 PRIMARY KEY(organization_id,user_id), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE mi.brand_workspaces (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), brand_id uuid GENERATED ALWAYS AS(id) STORED UNIQUE,
 organization_id uuid NOT NULL REFERENCES mi.organizations(id), name text NOT NULL,
 website_url text NOT NULL CHECK(website_url ~ '^https?://'), intake jsonb NOT NULL CHECK(jsonb_typeof(intake)='object'),
 status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived','deletion_requested')),
 created_by uuid NOT NULL REFERENCES identity.users(id), created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(id,organization_id)
);
CREATE TABLE mi.brand_memberships (
 brand_id uuid NOT NULL, organization_id uuid NOT NULL, user_id uuid NOT NULL,
 scopes text[] NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(brand_id,user_id), FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(organization_id,user_id) REFERENCES mi.organization_memberships(organization_id,user_id)
);
CREATE TABLE identity.invites (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL REFERENCES mi.organizations(id),
 brand_id uuid NOT NULL, email text NOT NULL CHECK(email=lower(email)),
 role text NOT NULL CHECK(role IN ('admin','team','client')),
 scopes text[] NOT NULL DEFAULT '{}', token_hash text NOT NULL UNIQUE,
 expires_at timestamptz NOT NULL, accepted_at timestamptz, invited_by uuid NOT NULL REFERENCES identity.users(id),
 created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id)
);
-- Narrow definer predicates avoid recursive membership policies. No dynamic SQL.
GRANT SELECT ON mi.organizations,mi.organization_memberships,mi.brand_workspaces,mi.brand_memberships,identity.users TO mi_guard;
CREATE FUNCTION mi.org_role(oid uuid) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 SELECT m.role FROM mi.organization_memberships m JOIN identity.users u ON u.id=m.user_id
 WHERE m.organization_id=oid AND m.user_id=mi.actor_id() AND NOT u.disabled $$;
ALTER FUNCTION mi.org_role(uuid) OWNER TO mi_guard;
CREATE FUNCTION mi.can_org_admin(oid uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT coalesce(mi.org_role(oid) IN ('founder','admin'),false) $$;
CREATE FUNCTION mi.has_brand_scope(bid uuid, requested text) RETURNS boolean
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 SELECT coalesce(EXISTS(SELECT 1 FROM mi.brand_workspaces b JOIN mi.organization_memberships m
 ON m.organization_id=b.organization_id JOIN identity.users u ON u.id=m.user_id
 LEFT JOIN mi.brand_memberships bm ON bm.brand_id=b.id AND bm.user_id=m.user_id
 WHERE b.id=bid AND m.user_id=mi.actor_id() AND NOT u.disabled AND (
 m.role IN ('founder','admin') OR (bm.user_id IS NOT NULL AND (
 (m.role='client' AND requested IN ('brand:read','onboarding:read','onboarding:write','published:read','approval:decide')) OR
 (m.role IN ('team','service') AND (requested=ANY(bm.scopes)))
 )))),false) $$;
ALTER FUNCTION mi.has_brand_scope(uuid,text) OWNER TO mi_guard;
CREATE FUNCTION mi.staff_brand(bid uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT mi.has_brand_scope(bid,'internal:read') $$;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA mi FROM PUBLIC;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA mi TO mi_app,mi_guard;
ALTER TABLE identity.users ENABLE ROW LEVEL SECURITY; ALTER TABLE identity.users FORCE ROW LEVEL SECURITY;
CREATE POLICY users_self ON identity.users TO mi_app USING(id=mi.actor_id()) WITH CHECK(id=mi.actor_id() AND NOT is_service);
GRANT SELECT(id,email,full_name,disabled,is_service,created_at),INSERT ON identity.users TO mi_app;
ALTER TABLE identity.sessions ENABLE ROW LEVEL SECURITY; ALTER TABLE identity.sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY sessions_self ON identity.sessions TO mi_app USING(user_id=mi.actor_id()) WITH CHECK(user_id=mi.actor_id());
GRANT SELECT,INSERT,UPDATE ON identity.sessions TO mi_app;
ALTER TABLE mi.organizations ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.organizations FORCE ROW LEVEL SECURITY;
CREATE POLICY org_read ON mi.organizations FOR SELECT TO mi_app USING(mi.org_role(id) IS NOT NULL OR owner_id=mi.actor_id());
CREATE POLICY org_insert ON mi.organizations FOR INSERT TO mi_app WITH CHECK(owner_id=mi.actor_id());
GRANT SELECT,INSERT ON mi.organizations TO mi_app;
ALTER TABLE mi.organization_memberships ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.organization_memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY org_member_read ON mi.organization_memberships FOR SELECT TO mi_app USING(user_id=mi.actor_id() OR mi.can_org_admin(organization_id));
CREATE POLICY org_member_insert ON mi.organization_memberships FOR INSERT TO mi_app WITH CHECK(mi.can_org_admin(organization_id) OR (user_id=mi.actor_id() AND role='founder' AND organization_id IN(SELECT id FROM mi.organizations WHERE owner_id=mi.actor_id())));
GRANT SELECT,INSERT ON mi.organization_memberships TO mi_app;
ALTER TABLE mi.brand_workspaces ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.brand_workspaces FORCE ROW LEVEL SECURITY;
CREATE POLICY brands_read ON mi.brand_workspaces FOR SELECT TO mi_app USING(mi.has_brand_scope(id,'brand:read') OR mi.staff_brand(id));
CREATE POLICY brands_insert ON mi.brand_workspaces FOR INSERT TO mi_app WITH CHECK(mi.can_org_admin(organization_id) AND created_by=mi.actor_id());
CREATE POLICY brands_update ON mi.brand_workspaces FOR UPDATE TO mi_app USING(mi.can_org_admin(organization_id)) WITH CHECK(mi.can_org_admin(organization_id));
GRANT SELECT,INSERT,UPDATE ON mi.brand_workspaces TO mi_app;
ALTER TABLE mi.brand_memberships ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.brand_memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY brand_member_read ON mi.brand_memberships FOR SELECT TO mi_app USING(user_id=mi.actor_id() OR mi.can_org_admin(organization_id));
CREATE POLICY brand_member_insert ON mi.brand_memberships FOR INSERT TO mi_app WITH CHECK(mi.can_org_admin(organization_id));
GRANT SELECT,INSERT ON mi.brand_memberships TO mi_app;
ALTER TABLE identity.invites ENABLE ROW LEVEL SECURITY; ALTER TABLE identity.invites FORCE ROW LEVEL SECURITY;
CREATE POLICY invite_admin ON identity.invites TO mi_app USING(mi.can_org_admin(organization_id)) WITH CHECK(mi.can_org_admin(organization_id) AND invited_by=mi.actor_id());
GRANT SELECT,INSERT ON identity.invites TO mi_app;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
CREATE FUNCTION mi.digest(bytea,text) RETURNS bytea LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE SET search_path=pg_catalog AS 'SELECT "extensions".digest($1,$2)';
CREATE FUNCTION mi.digest(text,text) RETURNS bytea LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE SET search_path=pg_catalog AS 'SELECT "extensions".digest($1,$2)';
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
CREATE POLICY managed_guard_read ON "identity"."users" FOR SELECT TO mi_guard USING(true);
CREATE POLICY managed_guard_read ON "mi"."organizations" FOR SELECT TO mi_guard USING(true);
CREATE POLICY managed_guard_read ON "mi"."organization_memberships" FOR SELECT TO mi_guard USING(true);
CREATE POLICY managed_guard_read ON "mi"."brand_workspaces" FOR SELECT TO mi_guard USING(true);
CREATE POLICY managed_guard_read ON "mi"."brand_memberships" FOR SELECT TO mi_guard USING(true);
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('001_identity_tenancy.sql','fe3f77ac5b0b8a506f3f1d8971fc948b69dfb38c70cd9216a29aac1acae86f49','supabase','6bb41ca109bad3a870de13f1770ea7e87f75a17a122be38694806180607d72d4');
-- Source: 002_domain.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

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
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('002_domain.sql','ecb597d107602ff0dcd917a1012c63fa3517fd1dbbd141a0315bf2377e0a5726','supabase','1800fc08d82da5e3b94bcc9f0fd8de55afa8e1e8e1a00210f26bb220eb1ad496');
-- Source: 003_security_audit.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

CREATE TABLE identity.auth_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), actor_id uuid REFERENCES identity.users(id),
 event text NOT NULL, subject_hash text, session_id uuid, success boolean NOT NULL,
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
GRANT INSERT ON identity.auth_events TO mi_app;
CREATE FUNCTION identity.login_lookup(address text) RETURNS TABLE(id uuid,password_hash text,disabled boolean)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,identity AS $$
 SELECT u.id,u.password_hash,u.disabled FROM identity.users u WHERE u.email=lower(address) AND NOT u.is_service $$;
CREATE FUNCTION identity.refresh_lookup(token_digest text) RETURNS SETOF identity.sessions
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,identity AS $$
 SELECT * FROM identity.sessions WHERE refresh_hash=token_digest FOR UPDATE $$;
CREATE FUNCTION identity.invite_lookup(token_digest text) RETURNS SETOF identity.invites
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,identity AS $$
 SELECT * FROM identity.invites WHERE token_hash=token_digest FOR UPDATE $$;
CREATE FUNCTION identity.accept_invite(token_digest text) RETURNS uuid
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,identity,mi AS $$
 DECLARE invitation identity.invites; account_email text;
 BEGIN
 SELECT * INTO invitation FROM identity.invites WHERE token_hash=token_digest FOR UPDATE;
 IF invitation.id IS NULL OR invitation.accepted_at IS NOT NULL OR invitation.expires_at<=now() THEN
 RAISE EXCEPTION 'invalid invitation' USING ERRCODE='22023'; END IF;
 SELECT email INTO account_email FROM identity.users WHERE id=mi.actor_id() AND NOT disabled AND NOT is_service;
 IF account_email IS DISTINCT FROM invitation.email THEN RAISE EXCEPTION 'invitation identity mismatch' USING ERRCODE='42501'; END IF;
 INSERT INTO mi.organization_memberships(organization_id,user_id,role) VALUES(invitation.organization_id,mi.actor_id(),invitation.role)
 ON CONFLICT(organization_id,user_id) DO NOTHING;
 INSERT INTO mi.brand_memberships(organization_id,brand_id,user_id,scopes)
 VALUES(invitation.organization_id,invitation.brand_id,mi.actor_id(),invitation.scopes)
 ON CONFLICT(brand_id,user_id) DO NOTHING;
 UPDATE identity.invites SET accepted_at=now() WHERE id=invitation.id;
 RETURN invitation.brand_id;
 END $$;
CREATE FUNCTION identity.check_attempt(bucket_key text) RETURNS boolean
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,identity AS $$
 DECLARE a identity.auth_attempts;
 BEGIN
 INSERT INTO identity.auth_attempts(bucket) VALUES(bucket_key) ON CONFLICT DO NOTHING;
 SELECT * INTO a FROM identity.auth_attempts WHERE bucket=bucket_key FOR UPDATE;
 IF a.window_start<now()-interval '15 minutes' THEN
 UPDATE identity.auth_attempts SET failures=0,window_start=now(),blocked_until=NULL WHERE bucket=bucket_key;
 RETURN true;
 END IF;
 RETURN a.blocked_until IS NULL OR a.blocked_until<=now();
 END $$;
CREATE FUNCTION identity.record_attempt(bucket_key text, ok boolean) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,identity AS $$
 BEGIN
 UPDATE identity.auth_attempts SET failures=CASE WHEN ok THEN 0 ELSE failures+1 END,
 blocked_until=CASE WHEN NOT ok AND failures>=4 THEN now()+interval '15 minutes' ELSE blocked_until END
 WHERE bucket=bucket_key;
 END $$;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA identity FROM PUBLIC;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA identity TO mi_app;
CREATE FUNCTION mi.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN RAISE EXCEPTION 'append only record' USING ERRCODE='42501'; END $$;
CREATE TRIGGER immutable_auth_events BEFORE UPDATE OR DELETE ON identity.auth_events FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
CREATE FUNCTION mi.audit_chain() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi,public AS $$
 BEGIN
 IF mi.actor_id() IS NULL THEN RAISE EXCEPTION 'actor context required' USING ERRCODE='42501'; END IF;
 NEW.actor_id:=mi.actor_id();
 PERFORM pg_advisory_xact_lock(hashtextextended(NEW.organization_id::text,0));
 SELECT entry_hash INTO NEW.previous_hash FROM mi.audit_logs WHERE organization_id=NEW.organization_id ORDER BY occurred_at DESC,id DESC LIMIT 1;
 NEW.occurred_at:=clock_timestamp();
 NEW.entry_hash:=encode(mi.digest(convert_to((to_jsonb(NEW)-'entry_hash')::text,'UTF8'),'sha256'),'hex');
 RETURN NEW;
 END $$;
CREATE TRIGGER audit_hash BEFORE INSERT ON mi.audit_logs FOR EACH ROW EXECUTE FUNCTION mi.audit_chain();
CREATE TRIGGER immutable_audit BEFORE UPDATE OR DELETE ON mi.audit_logs FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
CREATE FUNCTION mi.record_change() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi,public AS $$
 DECLARE row_data jsonb; old_data jsonb;
 BEGIN
 IF TG_OP='DELETE' THEN row_data:=to_jsonb(OLD); ELSE row_data:=to_jsonb(NEW); END IF;
 IF TG_OP='UPDATE' THEN old_data:=to_jsonb(OLD); END IF;
 INSERT INTO mi.audit_logs(organization_id,brand_id,actor_id,action,entity_type,entity_id,input_hash,output_hash)
 VALUES((row_data->>'organization_id')::uuid,(row_data->>'brand_id')::uuid,mi.actor_id(),TG_OP,TG_TABLE_NAME,(row_data->>'id')::uuid,
 CASE WHEN old_data IS NOT NULL THEN encode(mi.digest(old_data::text,'sha256'),'hex') END,
 encode(mi.digest(row_data::text,'sha256'),'hex'));
 RETURN NULL;
 END $$;
CREATE FUNCTION mi.guard_approval() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
 IF OLD.decision<>'pending' OR NEW.decision='pending' OR
 (to_jsonb(NEW)-ARRAY['decision','reason','decided_by','decided_at']) IS DISTINCT FROM
 (to_jsonb(OLD)-ARRAY['decision','reason','decided_by','decided_at']) THEN
 RAISE EXCEPTION 'approval target is immutable' USING ERRCODE='42501'; END IF;
 IF NEW.decided_by IS DISTINCT FROM mi.actor_id() OR NEW.assigned_to IS DISTINCT FROM mi.actor_id() OR
 coalesce(mi.org_role(NEW.organization_id),'') NOT IN('founder','admin','client') THEN
 RAISE EXCEPTION 'human assigned approver required' USING ERRCODE='42501'; END IF;
 RETURN NEW;
 END $$;
CREATE TRIGGER approval_decision_guard BEFORE UPDATE ON mi.approvals FOR EACH ROW EXECUTE FUNCTION mi.guard_approval();
CREATE TRIGGER immutable_approval_delete BEFORE DELETE ON mi.approvals FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
CREATE FUNCTION mi.disabled_integration() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
 IF NEW.status<>'disabled' OR NEW.access_mode<>'read' THEN
 RAISE EXCEPTION 'live integrations are disabled in phase 1' USING ERRCODE='42501'; END IF;
 RETURN NEW;
 END $$;
CREATE TRIGGER phase_one_integrations BEFORE INSERT OR UPDATE ON mi.integrations FOR EACH ROW EXECUTE FUNCTION mi.disabled_integration();
ALTER TABLE mi.artifacts ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.artifacts FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.artifacts FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.artifacts FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'artifact:write'));
GRANT SELECT,INSERT ON mi.artifacts TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.artifacts FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.brand_profiles ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.brand_profiles FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.brand_profiles FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.brand_profiles FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'profile:write'));
GRANT SELECT,INSERT ON mi.brand_profiles TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.brand_profiles FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.research_sources ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.research_sources FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.research_sources FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.research_sources FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'crawl:run'));
GRANT SELECT,INSERT ON mi.research_sources TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.research_sources FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.crawl_pages ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.crawl_pages FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.crawl_pages FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.crawl_pages FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'crawl:run'));
GRANT SELECT,INSERT ON mi.crawl_pages TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.crawl_pages FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.research_dossiers ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.research_dossiers FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.research_dossiers FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.research_dossiers FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'workflow:run'));
GRANT SELECT,INSERT ON mi.research_dossiers TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.research_dossiers FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.competitor_profiles ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.competitor_profiles FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.competitor_profiles FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.competitor_profiles FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'workflow:run'));
GRANT SELECT,INSERT ON mi.competitor_profiles TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.competitor_profiles FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.audience_segments ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.audience_segments FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.audience_segments FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.audience_segments FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'workflow:run'));
GRANT SELECT,INSERT ON mi.audience_segments TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.audience_segments FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.strategy_options ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.strategy_options FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.strategy_options FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.strategy_options FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'workflow:run'));
GRANT SELECT,INSERT ON mi.strategy_options TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.strategy_options FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.red_team_reviews ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.red_team_reviews FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.red_team_reviews FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.red_team_reviews FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'workflow:run'));
GRANT SELECT,INSERT ON mi.red_team_reviews TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.red_team_reviews FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.strategy_approvals ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.strategy_approvals FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.strategy_approvals FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.strategy_approvals FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'workflow:run'));
GRANT SELECT,INSERT ON mi.strategy_approvals TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.strategy_approvals FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.campaign_plans ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.campaign_plans FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.campaign_plans FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.campaign_plans FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'workflow:run'));
GRANT SELECT,INSERT ON mi.campaign_plans TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.campaign_plans FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.calendar_tasks ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.calendar_tasks FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.calendar_tasks FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.calendar_tasks FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'workflow:run'));
GRANT SELECT,INSERT ON mi.calendar_tasks TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.calendar_tasks FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.task_dependencies ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.task_dependencies FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.task_dependencies FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.task_dependencies FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'workflow:run'));
GRANT SELECT,INSERT ON mi.task_dependencies TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.task_dependencies FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.creative_briefs ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.creative_briefs FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.creative_briefs FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.creative_briefs FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'workflow:run'));
GRANT SELECT,INSERT ON mi.creative_briefs TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.creative_briefs FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.creative_assets ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.creative_assets FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.creative_assets FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.creative_assets FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'assets:write'));
GRANT SELECT,INSERT ON mi.creative_assets TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.creative_assets FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.prompt_templates ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.prompt_templates FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.prompt_templates FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.prompt_templates FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'settings:write'));
GRANT SELECT,INSERT ON mi.prompt_templates TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.prompt_templates FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.prompt_runs ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.prompt_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.prompt_runs FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.prompt_runs FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'workflow:run'));
GRANT SELECT,INSERT ON mi.prompt_runs TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.prompt_runs FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.agent_runs ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.agent_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.agent_runs FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.agent_runs FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'workflow:run'));
GRANT SELECT,INSERT ON mi.agent_runs TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.agent_runs FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.agent_outputs ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.agent_outputs FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.agent_outputs FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.agent_outputs FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'workflow:run'));
GRANT SELECT,INSERT ON mi.agent_outputs TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.agent_outputs FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.performance_snapshots ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.performance_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.performance_snapshots FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.performance_snapshots FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'workflow:run'));
GRANT SELECT,INSERT ON mi.performance_snapshots TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.performance_snapshots FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.recommendations ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.recommendations FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.recommendations FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.recommendations FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'workflow:run'));
GRANT SELECT,INSERT ON mi.recommendations TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.recommendations FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.reports ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.reports FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.reports FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.reports FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'workflow:run'));
GRANT SELECT,INSERT ON mi.reports TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.reports FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.presentations ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.presentations FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_read ON mi.presentations FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_insert ON mi.presentations FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'workflow:run'));
GRANT SELECT,INSERT ON mi.presentations TO mi_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON mi.presentations FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.workflow_instances ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.workflow_instances FORCE ROW LEVEL SECURITY;
CREATE POLICY scoped_read ON mi.workflow_instances FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_write ON mi.workflow_instances FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'crawl:run'));
CREATE POLICY scoped_update ON mi.workflow_instances FOR UPDATE TO mi_app USING(mi.has_brand_scope(brand_id,'crawl:run')) WITH CHECK(mi.has_brand_scope(brand_id,'crawl:run'));
GRANT SELECT,INSERT,UPDATE ON mi.workflow_instances TO mi_app;
ALTER TABLE mi.jobs ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY scoped_read ON mi.jobs FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_write ON mi.jobs FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'crawl:run'));
CREATE POLICY scoped_update ON mi.jobs FOR UPDATE TO mi_app USING(mi.has_brand_scope(brand_id,'crawl:run')) WITH CHECK(mi.has_brand_scope(brand_id,'crawl:run'));
GRANT SELECT,INSERT,UPDATE ON mi.jobs TO mi_app;
ALTER TABLE mi.idempotency_keys ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.idempotency_keys FORCE ROW LEVEL SECURITY;
CREATE POLICY scoped_read ON mi.idempotency_keys FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY scoped_write ON mi.idempotency_keys FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'crawl:run'));
CREATE POLICY scoped_update ON mi.idempotency_keys FOR UPDATE TO mi_app USING(mi.has_brand_scope(brand_id,'crawl:run')) WITH CHECK(mi.has_brand_scope(brand_id,'crawl:run'));
GRANT SELECT,INSERT,UPDATE ON mi.idempotency_keys TO mi_app;
ALTER TABLE mi.brand_memberships ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.brand_memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY bm_read ON mi.brand_memberships FOR SELECT TO mi_app USING(user_id=mi.actor_id() OR mi.can_org_admin(organization_id));
CREATE POLICY bm_insert ON mi.brand_memberships FOR INSERT TO mi_app WITH CHECK(mi.can_org_admin(organization_id));
GRANT SELECT,INSERT ON mi.brand_memberships TO mi_app;
ALTER TABLE mi.onboarding_responses ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.onboarding_responses FORCE ROW LEVEL SECURITY;
CREATE POLICY onboarding_read ON mi.onboarding_responses FOR SELECT TO mi_app USING(mi.staff_brand(brand_id) OR (respondent_id=mi.actor_id() AND mi.has_brand_scope(brand_id,'onboarding:read')));
CREATE POLICY onboarding_insert ON mi.onboarding_responses FOR INSERT TO mi_app WITH CHECK(respondent_id=mi.actor_id() AND mi.has_brand_scope(brand_id,'onboarding:write'));
GRANT SELECT,INSERT ON mi.onboarding_responses TO mi_app;
CREATE TRIGGER immutable_onboarding BEFORE UPDATE OR DELETE ON mi.onboarding_responses FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.workflow_events ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.workflow_events FORCE ROW LEVEL SECURITY;
CREATE POLICY event_read ON mi.workflow_events FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
GRANT SELECT ON mi.workflow_events TO mi_app;
CREATE TRIGGER immutable_workflow_event BEFORE UPDATE OR DELETE ON mi.workflow_events FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.audit_logs ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.audit_logs FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_read ON mi.audit_logs FOR SELECT TO mi_app USING(mi.can_org_admin(organization_id));
GRANT SELECT ON mi.audit_logs TO mi_app;
ALTER TABLE mi.integrations ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.integrations FORCE ROW LEVEL SECURITY;
CREATE POLICY integration_admin ON mi.integrations TO mi_app USING(mi.can_org_admin(organization_id)) WITH CHECK(mi.can_org_admin(organization_id));
GRANT SELECT,INSERT,UPDATE ON mi.integrations TO mi_app;
ALTER TABLE mi.integration_credentials ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.integration_credentials FORCE ROW LEVEL SECURITY;
-- Deliberately no runtime grants or policies for credentials before a connection exists.
ALTER TABLE mi.approvals ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.approvals FORCE ROW LEVEL SECURITY;
CREATE POLICY approval_read ON mi.approvals FOR SELECT TO mi_app USING(mi.staff_brand(brand_id) OR (assigned_to=mi.actor_id() AND mi.has_brand_scope(brand_id,'approval:decide')));
CREATE POLICY approval_insert ON mi.approvals FOR INSERT TO mi_app WITH CHECK(mi.can_org_admin(organization_id) AND decision='pending' AND decided_by IS NULL);
CREATE POLICY approval_update ON mi.approvals FOR UPDATE TO mi_app USING(assigned_to=mi.actor_id() AND mi.has_brand_scope(brand_id,'approval:decide')) WITH CHECK(assigned_to=mi.actor_id() AND mi.has_brand_scope(brand_id,'approval:decide'));
GRANT SELECT,INSERT,UPDATE ON mi.approvals TO mi_app;
ALTER TABLE mi.published_deliverables ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.published_deliverables FORCE ROW LEVEL SECURITY;
CREATE POLICY published_read ON mi.published_deliverables FOR SELECT TO mi_app USING(mi.staff_brand(brand_id) OR mi.has_brand_scope(brand_id,'published:read'));
-- Publishing is unavailable in Phase 1. There is no runtime INSERT grant.
GRANT SELECT ON mi.published_deliverables TO mi_app;
CREATE TRIGGER immutable_published BEFORE UPDATE OR DELETE ON mi.published_deliverables FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
ALTER TABLE mi.notifications ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.notifications FORCE ROW LEVEL SECURITY;
CREATE POLICY notification_read ON mi.notifications FOR SELECT TO mi_app USING(recipient_id=mi.actor_id() AND mi.has_brand_scope(brand_id,'brand:read'));
CREATE POLICY notification_insert ON mi.notifications FOR INSERT TO mi_app WITH CHECK(mi.can_org_admin(organization_id) AND NOT external_delivery);
GRANT SELECT,INSERT ON mi.notifications TO mi_app;
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.brand_workspaces FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.brand_memberships FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.workflow_instances FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.workflow_events FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.jobs FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.idempotency_keys FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.onboarding_responses FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.approvals FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.integrations FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.integration_credentials FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.published_deliverables FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.notifications FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.artifacts FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.brand_profiles FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.research_sources FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.crawl_pages FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.research_dossiers FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.competitor_profiles FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.audience_segments FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.strategy_options FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.red_team_reviews FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.strategy_approvals FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.campaign_plans FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.calendar_tasks FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.task_dependencies FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.creative_briefs FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.creative_assets FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.prompt_templates FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.prompt_runs FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.agent_runs FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.agent_outputs FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.performance_snapshots FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.recommendations FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.reports FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE TRIGGER audit_change AFTER INSERT OR UPDATE OR DELETE ON mi.presentations FOR EACH ROW EXECUTE FUNCTION mi.record_change();
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA mi FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.actor_id(),mi.org_role(uuid),mi.can_org_admin(uuid),mi.has_brand_scope(uuid,text),mi.staff_brand(uuid) TO mi_app,mi_guard;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('003_security_audit.sql','c448efabc958f5936585534835e17f8e5e0aad3f181b4d3732d9b6d5410cb006','supabase','2c2bb1c98b9de45b930a31ea31b405a06fb996044156b8e3e7c673ecbe1b4f7d');
-- Source: 004_workflow_and_intake.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

ALTER TABLE mi.brand_workspaces ADD COLUMN creation_key text NOT NULL;
ALTER TABLE mi.brand_workspaces ADD COLUMN creation_hash text NOT NULL CHECK(length(creation_hash)=64);
CREATE UNIQUE INDEX brand_creation_idempotency ON mi.brand_workspaces(organization_id,created_by,creation_key);
ALTER TABLE mi.workflow_instances ADD COLUMN resume_state text;
CREATE TABLE mi.workflow_transition_rules(from_state text NOT NULL,event text NOT NULL,to_state text NOT NULL,PRIMARY KEY(from_state,event,to_state));
GRANT SELECT ON mi.workflow_transition_rules TO mi_app;
INSERT INTO mi.workflow_transition_rules VALUES
('awaiting_creative_approval','approve_creative','report_queued'),
('awaiting_creative_approval','cancel','cancelled'),
('awaiting_creative_approval','request_creative_revision','creative_queued'),
('awaiting_deliverable_approval','approve_deliverables','completed'),
('awaiting_deliverable_approval','cancel','cancelled'),
('awaiting_deliverable_approval','request_deliverable_revision','report_queued'),
('awaiting_inputs','cancel','cancelled'),
('awaiting_inputs','inputs_provided','crawl_queued'),
('awaiting_inputs','inputs_provided','creative_queued'),
('awaiting_inputs','inputs_provided','plan_queued'),
('awaiting_inputs','inputs_provided','red_team_queued'),
('awaiting_inputs','inputs_provided','report_queued'),
('awaiting_inputs','inputs_provided','research_queued'),
('awaiting_inputs','inputs_provided','strategy_queued'),
('awaiting_inputs','resume_after_review','crawl_queued'),
('awaiting_inputs','resume_after_review','creative_queued'),
('awaiting_inputs','resume_after_review','plan_queued'),
('awaiting_inputs','resume_after_review','red_team_queued'),
('awaiting_inputs','resume_after_review','report_queued'),
('awaiting_inputs','resume_after_review','research_queued'),
('awaiting_inputs','resume_after_review','strategy_queued'),
('awaiting_inputs','retry','crawl_queued'),
('awaiting_inputs','retry','creative_queued'),
('awaiting_inputs','retry','plan_queued'),
('awaiting_inputs','retry','red_team_queued'),
('awaiting_inputs','retry','report_queued'),
('awaiting_inputs','retry','research_queued'),
('awaiting_inputs','retry','strategy_queued'),
('awaiting_onboarding','cancel','cancelled'),
('awaiting_onboarding','queue_crawl','crawl_queued'),
('awaiting_onboarding','submit_onboarding','awaiting_profile_confirmation'),
('awaiting_plan_approval','approve_plan','creative_queued'),
('awaiting_plan_approval','cancel','cancelled'),
('awaiting_plan_approval','request_plan_revision','plan_queued'),
('awaiting_profile_confirmation','cancel','cancelled'),
('awaiting_profile_confirmation','confirm_profile','ready_for_research'),
('awaiting_research_confirmation','cancel','cancelled'),
('awaiting_research_confirmation','confirm_research','strategy_queued'),
('awaiting_strategy_selection','approve_strategy','plan_queued'),
('awaiting_strategy_selection','cancel','cancelled'),
('awaiting_strategy_selection','request_strategy_revision','strategy_queued'),
('brand_created','cancel','cancelled'),
('brand_created','queue_crawl','crawl_queued'),
('crawl_failed','cancel','cancelled'),
('crawl_failed','retry','crawl_queued'),
('crawl_queued','cancel','cancelled'),
('crawl_queued','start_crawl','crawling'),
('crawling','cancel','cancelled'),
('crawling','crawl_failed','crawl_failed'),
('crawling','crawl_succeeded','awaiting_onboarding'),
('crawling','request_inputs','awaiting_inputs'),
('crawling','run_failed','failed'),
('crawling','validation_failed','crawling'),
('crawling','validation_failed','needs_human_review'),
('creating_briefs','cancel','cancelled'),
('creating_briefs','creative_succeeded','awaiting_creative_approval'),
('creating_briefs','request_inputs','awaiting_inputs'),
('creating_briefs','run_failed','failed'),
('creating_briefs','validation_failed','creating_briefs'),
('creating_briefs','validation_failed','needs_human_review'),
('creative_queued','cancel','cancelled'),
('creative_queued','start_creative','creating_briefs'),
('failed','cancel','cancelled'),
('failed','inputs_provided','crawl_queued'),
('failed','inputs_provided','creative_queued'),
('failed','inputs_provided','plan_queued'),
('failed','inputs_provided','red_team_queued'),
('failed','inputs_provided','report_queued'),
('failed','inputs_provided','research_queued'),
('failed','inputs_provided','strategy_queued'),
('failed','resume_after_review','crawl_queued'),
('failed','resume_after_review','creative_queued'),
('failed','resume_after_review','plan_queued'),
('failed','resume_after_review','red_team_queued'),
('failed','resume_after_review','report_queued'),
('failed','resume_after_review','research_queued'),
('failed','resume_after_review','strategy_queued'),
('failed','retry','crawl_queued'),
('failed','retry','creative_queued'),
('failed','retry','plan_queued'),
('failed','retry','red_team_queued'),
('failed','retry','report_queued'),
('failed','retry','research_queued'),
('failed','retry','strategy_queued'),
('needs_human_review','cancel','cancelled'),
('needs_human_review','inputs_provided','crawl_queued'),
('needs_human_review','inputs_provided','creative_queued'),
('needs_human_review','inputs_provided','plan_queued'),
('needs_human_review','inputs_provided','red_team_queued'),
('needs_human_review','inputs_provided','report_queued'),
('needs_human_review','inputs_provided','research_queued'),
('needs_human_review','inputs_provided','strategy_queued'),
('needs_human_review','resume_after_review','crawl_queued'),
('needs_human_review','resume_after_review','creative_queued'),
('needs_human_review','resume_after_review','plan_queued'),
('needs_human_review','resume_after_review','red_team_queued'),
('needs_human_review','resume_after_review','report_queued'),
('needs_human_review','resume_after_review','research_queued'),
('needs_human_review','resume_after_review','strategy_queued'),
('needs_human_review','retry','crawl_queued'),
('needs_human_review','retry','creative_queued'),
('needs_human_review','retry','plan_queued'),
('needs_human_review','retry','red_team_queued'),
('needs_human_review','retry','report_queued'),
('needs_human_review','retry','research_queued'),
('needs_human_review','retry','strategy_queued'),
('plan_queued','cancel','cancelled'),
('plan_queued','start_plan','planning'),
('planning','cancel','cancelled'),
('planning','plan_succeeded','awaiting_plan_approval'),
('planning','request_inputs','awaiting_inputs'),
('planning','run_failed','failed'),
('planning','validation_failed','needs_human_review'),
('planning','validation_failed','planning'),
('preparing_deliverables','cancel','cancelled'),
('preparing_deliverables','report_succeeded','awaiting_deliverable_approval'),
('preparing_deliverables','request_inputs','awaiting_inputs'),
('preparing_deliverables','run_failed','failed'),
('preparing_deliverables','validation_failed','needs_human_review'),
('preparing_deliverables','validation_failed','preparing_deliverables'),
('ready_for_research','cancel','cancelled'),
('ready_for_research','queue_research','research_queued'),
('red_team_queued','cancel','cancelled'),
('red_team_queued','start_red_team','reviewing_strategy'),
('report_queued','cancel','cancelled'),
('report_queued','start_report','preparing_deliverables'),
('research_queued','cancel','cancelled'),
('research_queued','start_research','researching'),
('researching','cancel','cancelled'),
('researching','request_inputs','awaiting_inputs'),
('researching','research_succeeded','awaiting_research_confirmation'),
('researching','run_failed','failed'),
('researching','validation_failed','needs_human_review'),
('researching','validation_failed','researching'),
('reviewing_strategy','cancel','cancelled'),
('reviewing_strategy','critique_succeeded','awaiting_strategy_selection'),
('reviewing_strategy','request_inputs','awaiting_inputs'),
('reviewing_strategy','run_failed','failed'),
('reviewing_strategy','validation_failed','needs_human_review'),
('reviewing_strategy','validation_failed','reviewing_strategy'),
('strategizing','cancel','cancelled'),
('strategizing','request_inputs','awaiting_inputs'),
('strategizing','run_failed','failed'),
('strategizing','strategy_succeeded','red_team_queued'),
('strategizing','validation_failed','needs_human_review'),
('strategizing','validation_failed','strategizing'),
('strategy_queued','cancel','cancelled'),
('strategy_queued','start_strategy','strategizing');
ALTER TABLE mi.workflow_instances ADD CONSTRAINT valid_workflow_state CHECK(state IN ('awaiting_creative_approval','awaiting_deliverable_approval','awaiting_inputs','awaiting_onboarding','awaiting_plan_approval','awaiting_profile_confirmation','awaiting_research_confirmation','awaiting_strategy_selection','brand_created','cancelled','completed','crawl_failed','crawl_queued','crawling','creating_briefs','creative_queued','failed','needs_human_review','plan_queued','planning','preparing_deliverables','ready_for_research','red_team_queued','report_queued','research_queued','researching','reviewing_strategy','strategizing','strategy_queued'));
CREATE FUNCTION mi.guard_workflow_transition() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 DECLARE event_name text; actual_status text; critical boolean;
 BEGIN
 event_name:=current_setting('app.workflow_event',true);
 IF NEW.brand_id<>OLD.brand_id OR NEW.organization_id<>OLD.organization_id OR NEW.version<>OLD.version+1 THEN
 RAISE EXCEPTION 'immutable workflow identity and monotonic version required' USING ERRCODE='22023'; END IF;
 IF event_name NOT IN ('queue_crawl','start_crawl','crawl_succeeded','crawl_failed','submit_onboarding','confirm_profile','retry','cancel','validation_failed','resume_after_review') THEN
 RAISE EXCEPTION 'workflow stage not enabled in phase 1' USING ERRCODE='42501'; END IF;
 IF NOT EXISTS(SELECT 1 FROM mi.workflow_transition_rules WHERE from_state=OLD.state AND event=event_name AND to_state=NEW.state) THEN
 RAISE EXCEPTION 'invalid workflow transition' USING ERRCODE='22023'; END IF;
 IF event_name IN('confirm_profile','cancel','resume_after_review','retry') AND NOT mi.can_org_admin(NEW.organization_id) THEN
 RAISE EXCEPTION 'agency administrator required' USING ERRCODE='42501'; END IF;
 IF event_name='validation_failed' AND (NEW.validation_failures<>least(2,OLD.validation_failures+1) OR
 (OLD.validation_failures>=1 AND NEW.state<>'needs_human_review') OR (OLD.validation_failures=0 AND NEW.state<>OLD.state)) THEN
 RAISE EXCEPTION 'validation failure threshold violated' USING ERRCODE='22023'; END IF;
 IF event_name='submit_onboarding' AND NOT EXISTS(SELECT 1 FROM mi.onboarding_responses WHERE brand_id=NEW.brand_id AND respondent_id=mi.actor_id() AND status='submitted') THEN
 RAISE EXCEPTION 'submitted onboarding required' USING ERRCODE='22023'; END IF;
 IF event_name='confirm_profile' THEN
 SELECT p.status,EXISTS(SELECT 1 FROM jsonb_array_elements(p.profile->'missing_inputs') x WHERE x->>'critical'='true')
 INTO actual_status,critical FROM mi.brand_profiles p WHERE p.brand_id=NEW.brand_id AND p.artifact_id=NEW.approved_profile_id
 AND p.version=(SELECT max(version) FROM mi.brand_profiles WHERE brand_id=NEW.brand_id);
 IF actual_status IS DISTINCT FROM 'confirmed' OR critical IS DISTINCT FROM false OR NOT EXISTS(
 SELECT 1 FROM mi.approvals WHERE brand_id=NEW.brand_id AND artifact_id=NEW.approved_profile_id AND decision='approved') THEN
 RAISE EXCEPTION 'current confirmed profile and human approval without critical gaps required' USING ERRCODE='22023'; END IF;
 END IF;
 RETURN NEW;
 END $$;
CREATE TRIGGER enforce_workflow_transition BEFORE UPDATE ON mi.workflow_instances FOR EACH ROW EXECUTE FUNCTION mi.guard_workflow_transition();
CREATE FUNCTION mi.record_workflow_transition() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 BEGIN
 INSERT INTO mi.workflow_events(organization_id,brand_id,workflow_id,event,from_state,to_state,workflow_version,actor_id)
 VALUES(NEW.organization_id,NEW.brand_id,NEW.id,current_setting('app.workflow_event',true),OLD.state,NEW.state,NEW.version,mi.actor_id());
 RETURN NULL;
 END $$;
CREATE TRIGGER record_workflow_transition AFTER UPDATE ON mi.workflow_instances FOR EACH ROW EXECUTE FUNCTION mi.record_workflow_transition();
GRANT SELECT ON mi.brand_profiles,mi.onboarding_responses,mi.workflow_instances TO mi_guard;
GRANT UPDATE ON mi.workflow_instances TO mi_guard;
CREATE FUNCTION mi.questionnaire(bid uuid) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 SELECT jsonb_build_object('brand_id',bid,'profile_version',p.version,'questions',p.profile->'onboarding_questions')
 FROM mi.brand_profiles p WHERE p.brand_id=bid AND mi.has_brand_scope(bid,'onboarding:read') ORDER BY p.version DESC LIMIT 1 $$;
ALTER FUNCTION mi.questionnaire(uuid) OWNER TO mi_guard;
CREATE FUNCTION mi.intake_open(bid uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 SELECT coalesce(EXISTS(SELECT 1 FROM mi.workflow_instances w WHERE w.brand_id=bid AND mi.has_brand_scope(bid,'onboarding:write') AND w.state IN('brand_created','crawl_queued','crawling','crawl_failed','awaiting_onboarding','awaiting_profile_confirmation','needs_human_review')),false) $$;
ALTER FUNCTION mi.intake_open(uuid) OWNER TO mi_guard;
CREATE FUNCTION mi.submit_intake(bid uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 DECLARE w mi.workflow_instances;
 BEGIN
 IF NOT mi.has_brand_scope(bid,'onboarding:write') THEN RAISE EXCEPTION 'not permitted' USING ERRCODE='42501'; END IF;
 SELECT * INTO w FROM mi.workflow_instances WHERE brand_id=bid ORDER BY created_at DESC LIMIT 1 FOR UPDATE;
 IF w.state='awaiting_profile_confirmation' THEN RETURN; END IF;
 IF w.state<>'awaiting_onboarding' THEN RAISE EXCEPTION 'crawl completion required before submission' USING ERRCODE='22023'; END IF;
 PERFORM set_config('app.workflow_event','submit_onboarding',true);
 UPDATE mi.workflow_instances SET state='awaiting_profile_confirmation',version=version+1 WHERE id=w.id;
 END $$;
ALTER FUNCTION mi.submit_intake(uuid) OWNER TO mi_guard;
REVOKE ALL ON FUNCTION mi.questionnaire(uuid),mi.intake_open(uuid),mi.submit_intake(uuid),mi.guard_workflow_transition(),mi.record_workflow_transition() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.questionnaire(uuid),mi.intake_open(uuid),mi.submit_intake(uuid) TO mi_app;
CREATE TABLE mi.tool_calls (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL,brand_id uuid NOT NULL,
 job_id uuid NOT NULL, actor_id uuid NOT NULL REFERENCES identity.users(id), endpoint text NOT NULL,
 method text NOT NULL CHECK(method='GET'), response_status integer, error_code text, latency_ms integer NOT NULL CHECK(latency_ms>=0),
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(job_id,brand_id) REFERENCES mi.jobs(id,brand_id)
);
ALTER TABLE mi.tool_calls ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.tool_calls FORCE ROW LEVEL SECURITY;
CREATE POLICY calls_read ON mi.tool_calls FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY calls_write ON mi.tool_calls FOR INSERT TO mi_app WITH CHECK(actor_id=mi.actor_id() AND mi.has_brand_scope(brand_id,'crawl:run'));
GRANT SELECT,INSERT ON mi.tool_calls TO mi_app;
CREATE TRIGGER immutable_call BEFORE UPDATE OR DELETE ON mi.tool_calls FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
CREATE TRIGGER audit_call AFTER INSERT ON mi.tool_calls FOR EACH ROW EXECUTE FUNCTION mi.record_change();
ALTER TABLE mi.calendar_tasks ADD UNIQUE(id,brand_id,plan_id);
ALTER TABLE mi.creative_briefs ADD FOREIGN KEY(task_id,brand_id,plan_id) REFERENCES mi.calendar_tasks(id,brand_id,plan_id);
ALTER TABLE mi.campaign_plans ADD COLUMN strategy_artifact_id uuid NOT NULL;
ALTER TABLE mi.campaign_plans ADD COLUMN approval_id uuid NOT NULL;
ALTER TABLE mi.campaign_plans ADD FOREIGN KEY(strategy_artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id);
ALTER TABLE mi.campaign_plans ADD FOREIGN KEY(approval_id,brand_id) REFERENCES mi.approvals(id,brand_id);
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
CREATE POLICY managed_guard_read ON "mi"."brand_profiles" FOR SELECT TO mi_guard USING(true);
CREATE POLICY managed_guard_read ON "mi"."onboarding_responses" FOR SELECT TO mi_guard USING(true);
CREATE POLICY managed_guard_read ON "mi"."workflow_instances" FOR SELECT TO mi_guard USING(true);
CREATE POLICY managed_guard_update ON mi.workflow_instances FOR UPDATE TO mi_guard USING(true) WITH CHECK(true);
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('004_workflow_and_intake.sql','241b2ea5ea8cb56331edb5f19b40d499cdbb3bf95075941dc4ec9506bc4b3ae3','supabase','3a12404c8349becf97179a209f2a90c955c5ccea0a5d98ec05651afcd2d8c5a5');
-- Source: 005_hardening.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA identity FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity.login_lookup(text),identity.refresh_lookup(text),identity.invite_lookup(text),
 identity.accept_invite(text),identity.check_attempt(text),identity.record_attempt(text,boolean) TO mi_app;
ALTER TABLE mi.audit_logs ADD COLUMN request_id uuid NOT NULL DEFAULT gen_random_uuid();
CREATE FUNCTION mi.log_access(bid uuid, requested text) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 DECLARE oid uuid;
 BEGIN
 IF NOT mi.has_brand_scope(bid,requested) THEN RAISE EXCEPTION 'not permitted' USING ERRCODE='42501'; END IF;
 SELECT organization_id INTO oid FROM mi.brand_workspaces WHERE id=bid;
 INSERT INTO mi.audit_logs(organization_id,brand_id,actor_id,action,entity_type,entity_id,request_id)
 VALUES(oid,bid,mi.actor_id(),'ACCESS:'||requested,'brand_workspaces',bid,
 coalesce(nullif(current_setting('app.request_id',true),'')::uuid,gen_random_uuid()));
 END $$;
REVOKE ALL ON FUNCTION mi.log_access(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.log_access(uuid,text) TO mi_app;
ALTER TABLE mi.creative_assets ADD CHECK(object_key LIKE 'brands/'||brand_id::text||'/%');
ALTER TABLE mi.calendar_tasks ADD FOREIGN KEY(organization_id,owner_id) REFERENCES mi.organization_memberships(organization_id,user_id);
CREATE FUNCTION mi.validate_strategy_approval() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 BEGIN
 IF NOT EXISTS(SELECT 1 FROM mi.approvals WHERE id=NEW.approval_id AND brand_id=NEW.brand_id
 AND artifact_id=NEW.strategy_artifact_id AND decision='approved') THEN
 RAISE EXCEPTION 'matching strategy approval required' USING ERRCODE='22023'; END IF;
 RETURN NEW;
 END $$;
CREATE TRIGGER strategy_approval_consistency BEFORE INSERT ON mi.strategy_approvals FOR EACH ROW EXECUTE FUNCTION mi.validate_strategy_approval();
CREATE FUNCTION mi.validate_plan_approval() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 BEGIN
 IF NOT EXISTS(SELECT 1 FROM mi.approvals WHERE id=NEW.approval_id AND brand_id=NEW.brand_id
 AND artifact_id=NEW.strategy_artifact_id AND decision='approved') THEN
 RAISE EXCEPTION 'matching strategy approval required' USING ERRCODE='22023'; END IF;
 RETURN NEW;
 END $$;
CREATE TRIGGER plan_approval_consistency BEFORE INSERT ON mi.campaign_plans FOR EACH ROW EXECUTE FUNCTION mi.validate_plan_approval();
REVOKE ALL ON FUNCTION mi.validate_strategy_approval(),mi.validate_plan_approval() FROM PUBLIC;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('005_hardening.sql','f929708f66dec0e4bdb2446a5a76c40bd20ce9c83fdc11add2005252e04162a0','supabase','f584826c4fe10129f17b9ba99aec1d297e65775b66afcb79d48478bd0565ea1b');
-- Source: 006_workspace_insert_visibility.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

-- INSERT RETURNING evaluates SELECT policy before a stable function can observe
-- the new workspace in its own table. Authorize administrators directly by org.
DROP POLICY brands_read ON mi.brand_workspaces;
CREATE POLICY brands_read ON mi.brand_workspaces FOR SELECT TO mi_app
 USING(mi.can_org_admin(organization_id) OR mi.has_brand_scope(id,'brand:read') OR mi.staff_brand(id));
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('006_workspace_insert_visibility.sql','70fc483b47ceda35763dd14059c071f21b3f6ee67d0463f636dec2c18f301e90','supabase','646fab401bff8b90c6f9db7c42466be945c659a7ae6f85889f9989cb6361fa93');
-- Source: 007_artifact_approval_binding.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

ALTER TABLE mi.artifacts ADD COLUMN content_sha256 text CHECK(content_sha256 IS NULL OR length(content_sha256)=64);
ALTER TABLE mi.approvals ADD COLUMN artifact_sha256 text CHECK(artifact_sha256 IS NULL OR length(artifact_sha256)=64);
ALTER TABLE mi.approvals ADD COLUMN requested_by uuid REFERENCES identity.users(id);
ALTER TABLE mi.approvals ADD COLUMN workflow_id uuid;
ALTER TABLE mi.approvals ADD FOREIGN KEY(workflow_id,brand_id) REFERENCES mi.workflow_instances(id,brand_id);
CREATE FUNCTION mi.bind_approval_target() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 BEGIN
 IF NEW.requested_by IS DISTINCT FROM mi.actor_id() OR NEW.workflow_id IS NULL OR NOT EXISTS(
 SELECT 1 FROM mi.artifacts a WHERE a.id=NEW.artifact_id AND a.brand_id=NEW.brand_id
 AND a.version=NEW.artifact_version AND a.content_sha256 IS NOT NULL AND a.content_sha256=NEW.artifact_sha256) THEN
 RAISE EXCEPTION 'exact artifact hash, workflow and requester required' USING ERRCODE='22023'; END IF;
 RETURN NEW;
 END $$;
CREATE TRIGGER approval_hash_binding BEFORE INSERT ON mi.approvals FOR EACH ROW EXECUTE FUNCTION mi.bind_approval_target();
REVOKE ALL ON FUNCTION mi.bind_approval_target() FROM PUBLIC;
-- Strictly ordered timestamps keep the org chain unambiguous even if clocks move.
CREATE OR REPLACE FUNCTION mi.audit_chain() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi,public AS $$
 DECLARE prior_time timestamptz;
 BEGIN
 IF mi.actor_id() IS NULL THEN RAISE EXCEPTION 'actor context required' USING ERRCODE='42501'; END IF;
 NEW.actor_id:=mi.actor_id();
 NEW.request_id:=coalesce(nullif(current_setting('app.request_id',true),'')::uuid,NEW.request_id);
 PERFORM pg_advisory_xact_lock(hashtextextended(NEW.organization_id::text,0));
 SELECT entry_hash,occurred_at INTO NEW.previous_hash,prior_time FROM mi.audit_logs
 WHERE organization_id=NEW.organization_id ORDER BY occurred_at DESC,id DESC LIMIT 1;
 NEW.occurred_at:=greatest(clock_timestamp(),prior_time+interval '1 microsecond');
 NEW.entry_hash:=encode(mi.digest(convert_to((to_jsonb(NEW)-'entry_hash')::text,'UTF8'),'sha256'),'hex');
 RETURN NEW;
 END $$;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('007_artifact_approval_binding.sql','57cd704ee870acc02ae7ecc47a471b93b42e6fa0c1e8f181f7b23ef84e11cbfb','supabase','f39123c497129035e9a790e0ff8510965af04620f84ed84d1a6160b8a06a6d24');
-- Source: 008_narrow_crawl_service_reads.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

-- A crawler need not read prompts, costs, research or other internal deliverables.
ALTER POLICY internal_read ON mi.brand_profiles USING(mi.staff_brand(brand_id) OR mi.has_brand_scope(brand_id,'profile:read'));
ALTER POLICY scoped_read ON mi.jobs USING(mi.staff_brand(brand_id) OR mi.has_brand_scope(brand_id,'job:read'));
ALTER POLICY scoped_read ON mi.workflow_instances USING(mi.staff_brand(brand_id) OR mi.has_brand_scope(brand_id,'workflow:read'));
ALTER TABLE mi.brand_profiles ADD CHECK(
 profile ? 'brand_id' AND profile->>'brand_id'=brand_id::text
 AND profile->>'version'=version::text AND profile->>'approval_status'=status
 AND profile->>'schema_version'='1.0');
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('008_narrow_crawl_service_reads.sql','9a61b53d7c8bc7d1abf0ff64d2b3d3f5370bb25c2aa373130cc2abf852cd962f','supabase','ea40c560f334c431b27a301a06a88bad841ea141ebcb6909077d189c162f3b7d');
-- Source: 009_workflow_initialization_and_audit_timezone.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

ALTER POLICY scoped_write ON mi.workflow_instances WITH CHECK(mi.can_org_admin(organization_id));
CREATE FUNCTION mi.validate_workflow_initialization() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
 IF NEW.state<>'brand_created' OR NEW.version<>1 OR NEW.validation_failures<>0 OR NEW.agent_calls<>0
 OR NEW.tokens_used<>0 OR NEW.approved_profile_id IS NOT NULL OR NEW.approved_strategy_id IS NOT NULL THEN
 RAISE EXCEPTION 'workflow must start at the intake boundary' USING ERRCODE='22023'; END IF;
 RETURN NEW;
 END $$;
CREATE TRIGGER workflow_initialization BEFORE INSERT ON mi.workflow_instances FOR EACH ROW EXECUTE FUNCTION mi.validate_workflow_initialization();
REVOKE ALL ON FUNCTION mi.validate_workflow_initialization() FROM PUBLIC;
ALTER FUNCTION mi.audit_chain() SET timezone TO 'UTC';
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('009_workflow_initialization_and_audit_timezone.sql','e8f6383485289c86aebe780c92fb648e838d65ef448b5e72826767ceb293c660','supabase','7682c03c168c5e108fcc836df0fd993441e80df855cb868b8037a65bb91024ce');
-- Source: 010_tool_attempt_provenance.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

ALTER TABLE mi.tool_calls ADD COLUMN attempt integer NOT NULL DEFAULT 1 CHECK(attempt BETWEEN 1 AND 3);
ALTER TABLE mi.tool_calls ADD COLUMN lease_id uuid;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('010_tool_attempt_provenance.sql','86ee247a6c1292d934689ecb2d90fb3604f2459a2cc7c8d230ef7c28592ba62e','supabase','60ef249fe937db271f47af7e76a122cc594b70323d8e19ec92b1bea1aea27e1c');
-- Source: 011_bound_service_identities.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='mi_api') THEN
 CREATE ROLE mi_api NOLOGIN NOBYPASSRLS IN ROLE mi_app;
 END IF;
END $$;
CREATE TABLE mi.service_principals (
 database_role name PRIMARY KEY,
 user_id uuid NOT NULL UNIQUE REFERENCES identity.users(id),
 created_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT ON mi.service_principals TO mi_guard;
CREATE OR REPLACE FUNCTION mi.actor_id() RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
 SET search_path=pg_catalog,mi AS $$
 SELECT coalesce((SELECT user_id FROM mi.service_principals WHERE database_role=session_user),
 nullif(current_setting('app.actor_id',true),'')::uuid) $$;
ALTER FUNCTION mi.actor_id() OWNER TO mi_guard;
CREATE FUNCTION mi.is_bound_service() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
 SET search_path=pg_catalog,mi AS $$
 SELECT EXISTS(SELECT 1 FROM mi.service_principals WHERE database_role=session_user) $$;
ALTER FUNCTION mi.is_bound_service() OWNER TO mi_guard;
REVOKE ALL ON FUNCTION mi.is_bound_service() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.actor_id(),mi.is_bound_service() TO mi_app;
-- Only the API role may handle passwords, invitations or interactive sessions.
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA identity FROM mi_app;
GRANT EXECUTE ON FUNCTION identity.login_lookup(text),identity.refresh_lookup(text),identity.invite_lookup(text),
 identity.accept_invite(text),identity.check_attempt(text),identity.record_attempt(text,boolean) TO mi_api;
REVOKE ALL ON identity.sessions,identity.invites,identity.auth_events FROM mi_app;
GRANT SELECT,INSERT,UPDATE ON identity.sessions TO mi_api;
GRANT SELECT,INSERT ON identity.invites TO mi_api;
GRANT INSERT ON identity.auth_events TO mi_api;
REVOKE INSERT ON identity.users,mi.organizations,mi.organization_memberships,mi.brand_memberships FROM mi_app;
GRANT INSERT ON identity.users,mi.organizations,mi.organization_memberships,mi.brand_memberships TO mi_api;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('011_bound_service_identities.sql','9647e7f841881d0839b6e3a4ef2382734644d8ae7764ee28fc9d261e748815d9','supabase','f32c6c0e4f38d1b8b92aee5d0a6ebb9b14e31570f093c53cd664e272ed246fde');
-- Source: 012_research_storage.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

ALTER TABLE mi.jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE mi.jobs ADD CHECK(kind IN('website_crawl','research'));
ALTER TABLE mi.jobs ADD COLUMN collection_complete boolean NOT NULL DEFAULT false;
ALTER TABLE mi.jobs ADD COLUMN source_limitations jsonb NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(source_limitations)='array');
DROP INDEX mi.one_active_crawl;
CREATE UNIQUE INDEX one_active_job_per_kind ON mi.jobs(brand_id,kind) WHERE status IN('queued','running','retry');
ALTER TABLE mi.workflow_instances ADD COLUMN approved_research_id uuid;
ALTER TABLE mi.workflow_instances ADD FOREIGN KEY(approved_research_id,brand_id) REFERENCES mi.artifacts(id,brand_id);
ALTER TABLE mi.workflow_instances ADD COLUMN tokens_reserved integer NOT NULL DEFAULT 0 CHECK(tokens_reserved>=0);
ALTER TABLE mi.workflow_instances ADD COLUMN cost_used_paise bigint NOT NULL DEFAULT 0 CHECK(cost_used_paise>=0);
ALTER TABLE mi.workflow_instances ADD COLUMN cost_reserved_paise bigint NOT NULL DEFAULT 0 CHECK(cost_reserved_paise>=0);
ALTER TABLE mi.workflow_instances ADD COLUMN budget_breached boolean NOT NULL DEFAULT false;
ALTER TABLE mi.workflow_instances ADD COLUMN pricing_complete boolean NOT NULL DEFAULT true;
DO $$ DECLARE c record; BEGIN
 FOR c IN SELECT conname FROM pg_constraint WHERE conrelid='mi.workflow_instances'::regclass AND pg_get_constraintdef(oid) LIKE '%tokens_used <= token_budget%'
 LOOP EXECUTE format('ALTER TABLE mi.workflow_instances DROP CONSTRAINT %I',c.conname); END LOOP;
END $$;
CREATE TABLE mi.research_evidence (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,brand_id uuid NOT NULL,
 job_id uuid NOT NULL,source_id uuid NOT NULL,area text NOT NULL,kind text NOT NULL,
 extracted_text text NOT NULL CHECK(length(extracted_text) BETWEEN 1 AND 12000),
 extracted_sha256 text NOT NULL CHECK(length(extracted_sha256)=64),
 confidence_cap numeric NOT NULL CHECK(confidence_cap BETWEEN 0 AND 1),created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(id,brand_id),UNIQUE(job_id,source_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(job_id,brand_id) REFERENCES mi.jobs(id,brand_id),
 FOREIGN KEY(source_id,brand_id) REFERENCES mi.research_sources(id,brand_id)
);
CREATE INDEX research_evidence_tenant ON mi.research_evidence(brand_id,job_id);
CREATE TABLE mi.model_dispatches (
 id uuid PRIMARY KEY,organization_id uuid NOT NULL,brand_id uuid NOT NULL,workflow_id uuid NOT NULL,job_id uuid NOT NULL,
 lease_id uuid NOT NULL,template_id uuid NOT NULL,profile_artifact_id uuid NOT NULL,
 state text NOT NULL CHECK(state IN('reserved','dispatched','settled','outcome_unknown','rejected')),
 provider text NOT NULL,model text NOT NULL,request jsonb NOT NULL CHECK(jsonb_typeof(request)='object'),
 request_sha256 text NOT NULL CHECK(length(request_sha256)=64),output_schema text NOT NULL,
 input_token_estimate integer NOT NULL CHECK(input_token_estimate>0),maximum_output_tokens integer NOT NULL CHECK(maximum_output_tokens>0),
 reserved_tokens integer NOT NULL CHECK(reserved_tokens>0),reserved_cost_paise bigint NOT NULL DEFAULT 0 CHECK(reserved_cost_paise>=0),
 input_tokens integer CHECK(input_tokens>=0),output_tokens integer CHECK(output_tokens>=0),cost_paise bigint CHECK(cost_paise>=0),
 pricing_as_of date,input_rate_paise_per_million bigint,output_rate_paise_per_million bigint,
 error_code text,latency_ms integer CHECK(latency_ms>=0),created_by uuid NOT NULL REFERENCES identity.users(id),
 created_at timestamptz NOT NULL DEFAULT now(),dispatched_at timestamptz,finished_at timestamptz,UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(workflow_id,brand_id) REFERENCES mi.workflow_instances(id,brand_id),
 FOREIGN KEY(job_id,brand_id) REFERENCES mi.jobs(id,brand_id),
 FOREIGN KEY(template_id,brand_id) REFERENCES mi.prompt_templates(id,brand_id),
 FOREIGN KEY(profile_artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id)
);
CREATE INDEX model_dispatches_tenant ON mi.model_dispatches(brand_id,job_id);
CREATE UNIQUE INDEX one_open_dispatch ON mi.model_dispatches(job_id) WHERE state IN('reserved','dispatched','outcome_unknown');
CREATE TABLE mi.research_reviews (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,brand_id uuid NOT NULL,
 dossier_id uuid NOT NULL,artifact_id uuid NOT NULL,artifact_sha256 text NOT NULL CHECK(length(artifact_sha256)=64),
 reviewer_id uuid NOT NULL REFERENCES identity.users(id),decision text NOT NULL CHECK(decision IN('approved','revision_requested')),
 reason text NOT NULL,resolutions jsonb NOT NULL CHECK(jsonb_typeof(resolutions)='object'),created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(id,brand_id),FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(dossier_id,brand_id) REFERENCES mi.research_dossiers(id,brand_id),
 FOREIGN KEY(artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id)
);
CREATE INDEX research_reviews_tenant ON mi.research_reviews(brand_id,dossier_id);
ALTER TABLE mi.research_evidence ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.research_evidence FORCE ROW LEVEL SECURITY;
CREATE POLICY evidence_read ON mi.research_evidence FOR SELECT TO mi_app USING(mi.staff_brand(brand_id) OR mi.has_brand_scope(brand_id,'research:run'));
CREATE POLICY evidence_write ON mi.research_evidence FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'research:run'));
GRANT SELECT,INSERT ON mi.research_evidence TO mi_app;
CREATE TRIGGER evidence_immutable BEFORE UPDATE OR DELETE ON mi.research_evidence FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
CREATE TRIGGER evidence_audit AFTER INSERT ON mi.research_evidence FOR EACH ROW EXECUTE FUNCTION mi.record_change();
ALTER TABLE mi.model_dispatches ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.model_dispatches FORCE ROW LEVEL SECURITY;
CREATE POLICY dispatch_read ON mi.model_dispatches FOR SELECT TO mi_app USING(mi.staff_brand(brand_id) OR mi.has_brand_scope(brand_id,'research:run'));
CREATE POLICY dispatch_insert ON mi.model_dispatches FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'research:run') AND created_by=mi.actor_id());
CREATE POLICY dispatch_update ON mi.model_dispatches FOR UPDATE TO mi_app USING(mi.has_brand_scope(brand_id,'research:run')) WITH CHECK(mi.has_brand_scope(brand_id,'research:run'));
GRANT SELECT,INSERT,UPDATE ON mi.model_dispatches TO mi_app;
CREATE TRIGGER dispatch_audit AFTER INSERT OR UPDATE ON mi.model_dispatches FOR EACH ROW EXECUTE FUNCTION mi.record_change();
ALTER TABLE mi.research_reviews ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.research_reviews FORCE ROW LEVEL SECURITY;
CREATE POLICY review_read ON mi.research_reviews FOR SELECT TO mi_app USING(mi.staff_brand(brand_id));
CREATE POLICY review_insert ON mi.research_reviews FOR INSERT TO mi_app WITH CHECK(mi.can_org_admin(organization_id) AND reviewer_id=mi.actor_id());
GRANT SELECT,INSERT ON mi.research_reviews TO mi_app;
CREATE TRIGGER review_immutable BEFORE UPDATE OR DELETE ON mi.research_reviews FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
CREATE TRIGGER review_audit AFTER INSERT ON mi.research_reviews FOR EACH ROW EXECUTE FUNCTION mi.record_change();
ALTER POLICY scoped_write ON mi.jobs WITH CHECK(mi.has_brand_scope(brand_id,'crawl:run') OR mi.has_brand_scope(brand_id,'research:run'));
ALTER POLICY scoped_update ON mi.jobs USING(mi.has_brand_scope(brand_id,'crawl:run') OR mi.has_brand_scope(brand_id,'research:run')) WITH CHECK(mi.has_brand_scope(brand_id,'crawl:run') OR mi.has_brand_scope(brand_id,'research:run'));
ALTER POLICY scoped_update ON mi.workflow_instances USING(mi.has_brand_scope(brand_id,'crawl:run') OR mi.has_brand_scope(brand_id,'research:run')) WITH CHECK(mi.has_brand_scope(brand_id,'crawl:run') OR mi.has_brand_scope(brand_id,'research:run'));
ALTER POLICY scoped_write ON mi.idempotency_keys WITH CHECK(actor_id=mi.actor_id() AND (mi.has_brand_scope(brand_id,'crawl:run') OR mi.has_brand_scope(brand_id,'research:run')));
ALTER POLICY internal_read ON mi.research_sources USING(mi.staff_brand(brand_id) OR mi.has_brand_scope(brand_id,'research:run'));
ALTER POLICY scoped_insert ON mi.research_sources WITH CHECK(mi.has_brand_scope(brand_id,'crawl:run') OR mi.has_brand_scope(brand_id,'research:run'));
ALTER POLICY internal_read ON mi.prompt_templates USING(mi.staff_brand(brand_id) OR mi.has_brand_scope(brand_id,'research:run'));
ALTER POLICY scoped_insert ON mi.prompt_templates WITH CHECK(mi.can_org_admin(organization_id));
ALTER POLICY internal_read ON mi.research_dossiers USING(mi.staff_brand(brand_id) OR mi.has_brand_scope(brand_id,'research:run'));
ALTER POLICY scoped_insert ON mi.research_dossiers WITH CHECK(mi.has_brand_scope(brand_id,'research:run'));
ALTER POLICY scoped_insert ON mi.competitor_profiles WITH CHECK(mi.has_brand_scope(brand_id,'research:run'));
ALTER POLICY scoped_insert ON mi.audience_segments WITH CHECK(mi.has_brand_scope(brand_id,'research:run'));
ALTER POLICY scoped_insert ON mi.prompt_runs WITH CHECK(mi.has_brand_scope(brand_id,'research:run'));
ALTER POLICY scoped_insert ON mi.agent_runs WITH CHECK(mi.has_brand_scope(brand_id,'research:run'));
ALTER POLICY scoped_insert ON mi.agent_outputs WITH CHECK(mi.has_brand_scope(brand_id,'research:run'));
ALTER TABLE mi.tool_calls DROP CONSTRAINT tool_calls_method_check;
ALTER TABLE mi.tool_calls ADD CHECK(method IN('GET','POST'));
ALTER TABLE mi.tool_calls ADD COLUMN request_sha256 text;
ALTER POLICY calls_write ON mi.tool_calls WITH CHECK(actor_id=mi.actor_id() AND (mi.has_brand_scope(brand_id,'crawl:run') OR mi.has_brand_scope(brand_id,'research:run')));
CREATE FUNCTION mi.guard_dispatch() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
 IF (to_jsonb(NEW)-ARRAY['state','input_tokens','output_tokens','cost_paise','error_code','latency_ms','dispatched_at','finished_at']) IS DISTINCT FROM
 (to_jsonb(OLD)-ARRAY['state','input_tokens','output_tokens','cost_paise','error_code','latency_ms','dispatched_at','finished_at'])
 OR OLD.state NOT IN('reserved','dispatched') THEN RAISE EXCEPTION 'immutable model request or completed dispatch' USING ERRCODE='42501'; END IF;
 RETURN NEW;
 END $$;
CREATE TRIGGER dispatch_guard BEFORE UPDATE ON mi.model_dispatches FOR EACH ROW EXECUTE FUNCTION mi.guard_dispatch();
REVOKE ALL ON FUNCTION mi.guard_dispatch() FROM PUBLIC;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('012_research_storage.sql','ae987ba7891d65f454f3e42f622c598f4cb8a1a74fe4ebaabebcf06544b6d6d5','supabase','f07fdad53578ce0a68c8cf5483b63bc198f6bfcdb63f1cc247763bf1c0787122');
-- Source: 013_research_workflow.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

ALTER TABLE mi.workflow_instances DROP CONSTRAINT valid_workflow_state;
ALTER TABLE mi.workflow_instances ADD CONSTRAINT valid_workflow_state CHECK(state IN ('awaiting_creative_approval','awaiting_deliverable_approval','awaiting_inputs','awaiting_onboarding','awaiting_plan_approval','awaiting_profile_confirmation','awaiting_research_confirmation','awaiting_strategy_selection','brand_created','cancelled','completed','crawl_failed','crawl_queued','crawling','creating_briefs','creative_queued','failed','needs_human_review','plan_queued','planning','preparing_deliverables','ready_for_research','ready_for_strategy','red_team_queued','report_queued','research_queued','researching','reviewing_strategy','strategizing','strategy_queued'));
DELETE FROM mi.workflow_transition_rules WHERE from_state='awaiting_research_confirmation' AND event='confirm_research';
INSERT INTO mi.workflow_transition_rules VALUES
('awaiting_research_confirmation','confirm_research','ready_for_strategy'),
('awaiting_research_confirmation','request_research_revision','ready_for_research'),
('researching','provider_outcome_unknown','needs_human_review'),
('researching','budget_exhausted','needs_human_review'),
('ready_for_strategy','cancel','cancelled');
DROP TRIGGER record_workflow_transition ON mi.workflow_instances;
CREATE TRIGGER record_workflow_transition AFTER UPDATE ON mi.workflow_instances
 FOR EACH ROW WHEN(OLD.state IS DISTINCT FROM NEW.state OR OLD.version IS DISTINCT FROM NEW.version)
 EXECUTE FUNCTION mi.record_workflow_transition();
CREATE OR REPLACE FUNCTION mi.guard_workflow_transition() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 DECLARE event_name text; actual_status text; critical boolean;
 BEGIN
 event_name:=current_setting('app.workflow_event',true);
 IF event_name='research_budget' THEN
 IF NOT mi.has_brand_scope(OLD.brand_id,'research:run') OR
 (to_jsonb(NEW)-ARRAY['agent_calls','tokens_used','tokens_reserved','cost_used_paise','cost_reserved_paise','budget_breached','pricing_complete']) IS DISTINCT FROM
 (to_jsonb(OLD)-ARRAY['agent_calls','tokens_used','tokens_reserved','cost_used_paise','cost_reserved_paise','budget_breached','pricing_complete']) OR
 NEW.agent_calls<OLD.agent_calls OR NEW.agent_calls>OLD.agent_calls+1 OR NEW.tokens_used<OLD.tokens_used OR NEW.cost_used_paise<OLD.cost_used_paise OR
 (OLD.budget_breached AND NOT NEW.budget_breached) OR (NOT OLD.pricing_complete AND NEW.pricing_complete) THEN
 RAISE EXCEPTION 'invalid budget mutation' USING ERRCODE='42501'; END IF;
 RETURN NEW;
 END IF;

 IF NEW.brand_id<>OLD.brand_id OR NEW.organization_id<>OLD.organization_id OR NEW.version<>OLD.version+1 THEN
 RAISE EXCEPTION 'immutable workflow identity and monotonic version required' USING ERRCODE='22023'; END IF;
 IF event_name NOT IN ('queue_crawl','start_crawl','crawl_succeeded','crawl_failed','submit_onboarding','confirm_profile','retry','cancel','validation_failed','resume_after_review','queue_research','start_research','research_succeeded','request_inputs','run_failed','provider_outcome_unknown','budget_exhausted','confirm_research','request_research_revision','inputs_provided') THEN
 RAISE EXCEPTION 'workflow stage not enabled in phase 1' USING ERRCODE='42501'; END IF;
 IF NOT EXISTS(SELECT 1 FROM mi.workflow_transition_rules WHERE from_state=OLD.state AND event=event_name AND to_state=NEW.state) THEN
 RAISE EXCEPTION 'invalid workflow transition' USING ERRCODE='22023'; END IF;
 IF event_name IN('confirm_profile','cancel','resume_after_review','retry','confirm_research','request_research_revision','inputs_provided') AND NOT mi.can_org_admin(NEW.organization_id) THEN
 RAISE EXCEPTION 'agency administrator required' USING ERRCODE='42501'; END IF;
 IF event_name='validation_failed' AND (NEW.validation_failures<>least(2,OLD.validation_failures+1) OR
 (OLD.validation_failures>=1 AND NEW.state<>'needs_human_review') OR (OLD.validation_failures=0 AND NEW.state<>OLD.state)) THEN
 RAISE EXCEPTION 'validation failure threshold violated' USING ERRCODE='22023'; END IF;
 IF event_name='submit_onboarding' AND NOT EXISTS(SELECT 1 FROM mi.onboarding_responses WHERE brand_id=NEW.brand_id AND respondent_id=mi.actor_id() AND status='submitted') THEN
 RAISE EXCEPTION 'submitted onboarding required' USING ERRCODE='22023'; END IF;
 IF event_name='confirm_profile' THEN
 SELECT p.status,EXISTS(SELECT 1 FROM jsonb_array_elements(p.profile->'missing_inputs') x WHERE x->>'critical'='true')
 INTO actual_status,critical FROM mi.brand_profiles p WHERE p.brand_id=NEW.brand_id AND p.artifact_id=NEW.approved_profile_id
 AND p.version=(SELECT max(version) FROM mi.brand_profiles WHERE brand_id=NEW.brand_id);
 IF actual_status IS DISTINCT FROM 'confirmed' OR critical IS DISTINCT FROM false OR NOT EXISTS(
 SELECT 1 FROM mi.approvals WHERE brand_id=NEW.brand_id AND artifact_id=NEW.approved_profile_id AND decision='approved') THEN
 RAISE EXCEPTION 'current confirmed profile and human approval without critical gaps required' USING ERRCODE='22023'; END IF;
 END IF;

 IF event_name IN('queue_research','start_research') AND (NEW.approved_profile_id IS NULL OR NOT EXISTS(
 SELECT 1 FROM mi.brand_profiles p WHERE p.brand_id=NEW.brand_id AND p.artifact_id=NEW.approved_profile_id AND p.status='confirmed'
 AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(p.profile->'missing_inputs') x WHERE x->>'critical'='true'))) THEN
 RAISE EXCEPTION 'confirmed intake required' USING ERRCODE='22023'; END IF;
 IF event_name='confirm_research' AND NOT EXISTS(
 SELECT 1 FROM mi.research_reviews r JOIN mi.research_dossiers d ON d.id=r.dossier_id AND d.brand_id=r.brand_id
 JOIN mi.artifacts a ON a.id=d.artifact_id AND a.brand_id=d.brand_id
 WHERE r.brand_id=NEW.brand_id AND r.artifact_id=NEW.approved_research_id AND r.reviewer_id=mi.actor_id()
 AND r.decision='approved' AND r.artifact_sha256=a.content_sha256 AND d.profile_artifact_id=NEW.approved_profile_id) THEN
 RAISE EXCEPTION 'matching human research review required' USING ERRCODE='22023'; END IF;
 IF NEW.approved_research_id IS DISTINCT FROM OLD.approved_research_id AND event_name<>'confirm_research' THEN
 RAISE EXCEPTION 'research approval cannot be forged' USING ERRCODE='42501'; END IF;
 IF NEW.approved_profile_id IS DISTINCT FROM OLD.approved_profile_id AND event_name<>'confirm_profile' THEN
 RAISE EXCEPTION 'profile approval cannot be forged' USING ERRCODE='42501'; END IF;
 RETURN NEW;
 END $$;

;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('013_research_workflow.sql','ff624ce8479bdcb8cec075d28640360cbdd239a4a4c1c69384deacfcc057e40d','supabase','9b4d38d262c06f400143b0c38b2595a9e3e3b89fb5cf192495e9b994f60c94bd');
-- Source: 014_research_dispatch_guards.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

DROP TRIGGER record_workflow_transition ON mi.workflow_instances;
CREATE TRIGGER record_workflow_transition AFTER UPDATE ON mi.workflow_instances FOR EACH ROW
 WHEN (OLD.state IS DISTINCT FROM NEW.state OR OLD.version IS DISTINCT FROM NEW.version)
 EXECUTE FUNCTION mi.record_workflow_transition();
CREATE FUNCTION mi.validate_dispatch_insert() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 BEGIN
 IF NOT EXISTS(SELECT 1 FROM mi.jobs j JOIN mi.workflow_instances w ON w.id=j.workflow_id AND w.brand_id=j.brand_id
 WHERE j.id=NEW.job_id AND j.brand_id=NEW.brand_id AND j.kind='research' AND j.status='running'
 AND j.lease_id=NEW.lease_id AND j.lease_expires_at>now() AND w.id=NEW.workflow_id AND w.state='researching'
 AND w.approved_profile_id=NEW.profile_artifact_id) THEN
 RAISE EXCEPTION 'current research lease and profile required' USING ERRCODE='22023'; END IF;
 RETURN NEW;
 END $$;
CREATE TRIGGER dispatch_insert_guard BEFORE INSERT ON mi.model_dispatches FOR EACH ROW EXECUTE FUNCTION mi.validate_dispatch_insert();
CREATE OR REPLACE FUNCTION mi.guard_dispatch() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
 IF (to_jsonb(NEW)-ARRAY['state','input_tokens','output_tokens','cost_paise','error_code','latency_ms','dispatched_at','finished_at']) IS DISTINCT FROM
 (to_jsonb(OLD)-ARRAY['state','input_tokens','output_tokens','cost_paise','error_code','latency_ms','dispatched_at','finished_at'])
 OR NOT ((OLD.state='reserved' AND NEW.state IN('dispatched','rejected')) OR
 (OLD.state='dispatched' AND NEW.state IN('settled','outcome_unknown','rejected'))) THEN
 RAISE EXCEPTION 'immutable or invalid dispatch transition' USING ERRCODE='42501'; END IF;
 IF NEW.state='settled' AND (NEW.input_tokens IS NULL OR NEW.output_tokens IS NULL OR NEW.finished_at IS NULL) THEN
 RAISE EXCEPTION 'observed usage is required for settlement' USING ERRCODE='22023'; END IF;
 RETURN NEW;
 END $$;
REVOKE ALL ON FUNCTION mi.validate_dispatch_insert(),mi.guard_dispatch() FROM PUBLIC;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('014_research_dispatch_guards.sql','b87ee1aa1410a05b2f5ce76760b34a3238c2d5d083d7f32f6737a6fab576bf6e','supabase','05b2f1240c20952fdbb1c149d9b0bbdcb554223190ad2b072da4f61b6deb8a7b');
-- Source: 015_research_artifact_reads.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

ALTER POLICY internal_read ON mi.artifacts USING(mi.staff_brand(brand_id) OR
 (kind IN('brand_profile','research_dossier') AND mi.has_brand_scope(brand_id,'research:run')));
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('015_research_artifact_reads.sql','9197a83f23ce46dbff04564a7caa2fdbdba9a169158f92a65e30c6b1ee21b102','supabase','b0e7af72c70442b3de553f358625fafaa4df57c1148f887845776498a6128463');
-- Source: 016_research_review_binding.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

ALTER TABLE mi.research_dossiers ADD COLUMN job_id uuid;
ALTER TABLE mi.research_dossiers ADD FOREIGN KEY(job_id,brand_id) REFERENCES mi.jobs(id,brand_id);
CREATE INDEX research_dossiers_job ON mi.research_dossiers(brand_id,job_id);
CREATE FUNCTION mi.guard_research_review() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 BEGIN
 IF NOT mi.can_org_admin(NEW.organization_id) OR NEW.reviewer_id IS DISTINCT FROM mi.actor_id() OR NOT EXISTS(
 SELECT 1 FROM mi.research_dossiers d JOIN mi.artifacts a ON a.id=d.artifact_id AND a.brand_id=d.brand_id
 JOIN mi.workflow_instances w ON w.brand_id=d.brand_id AND w.approved_profile_id=d.profile_artifact_id
 WHERE d.id=NEW.dossier_id AND d.brand_id=NEW.brand_id AND d.artifact_id=NEW.artifact_id
 AND a.content_sha256=NEW.artifact_sha256 AND w.state='awaiting_research_confirmation'
 AND a.version=(SELECT max(version) FROM mi.artifacts WHERE brand_id=NEW.brand_id AND kind='research_dossier')) THEN
 RAISE EXCEPTION 'current human research review target required' USING ERRCODE='22023'; END IF;
 RETURN NEW;
 END $$;
CREATE TRIGGER research_review_target BEFORE INSERT ON mi.research_reviews FOR EACH ROW EXECUTE FUNCTION mi.guard_research_review();
REVOKE ALL ON FUNCTION mi.guard_research_review() FROM PUBLIC;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('016_research_review_binding.sql','1c91a0da67a5795dc9d6f0acbf202953f5b107a63668120f01da30547e3ac57d','supabase','7511c2b0f25ec6880e893b3df436358d68fad90b7e1a021f119904762086c12f');
-- Source: 017_late_usage_reconciliation.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

CREATE OR REPLACE FUNCTION mi.guard_dispatch() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
 IF (to_jsonb(NEW)-ARRAY['state','input_tokens','output_tokens','cost_paise','error_code','latency_ms','dispatched_at','finished_at']) IS DISTINCT FROM
 (to_jsonb(OLD)-ARRAY['state','input_tokens','output_tokens','cost_paise','error_code','latency_ms','dispatched_at','finished_at'])
 OR NOT ((OLD.state='reserved' AND NEW.state IN('dispatched','rejected')) OR
 (OLD.state='dispatched' AND NEW.state IN('settled','outcome_unknown','rejected')) OR (OLD.state='outcome_unknown' AND NEW.state='settled')) THEN
 RAISE EXCEPTION 'immutable or invalid dispatch transition' USING ERRCODE='42501'; END IF;
 IF NEW.state='settled' AND (NEW.input_tokens IS NULL OR NEW.output_tokens IS NULL OR NEW.finished_at IS NULL) THEN
 RAISE EXCEPTION 'observed usage is required for settlement' USING ERRCODE='22023'; END IF;
 RETURN NEW;
 END $$;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('017_late_usage_reconciliation.sql','a203ddba7245cc6c232cb2a298ecd0c2ed01beaecd7aa812ec8f21a901c23526','supabase','d44e589fe5853b2acb7251a331b7cfb0f474d7a78cf20b91880daf1701109fcf');
-- Source: 018_research_semantic_review.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

-- Existing decisions remain historical. New approvals need explicit human semantic review.
-- NOT VALID avoids rewriting/revoking old append-only review records; new inserts are checked.
ALTER TABLE mi.research_reviews ADD CONSTRAINT research_semantic_review_required CHECK (
 decision <> 'approved' OR (
   coalesce(resolutions->'semantic_review'->'source_quality_checked' = 'true'::jsonb,false)
   AND coalesce(resolutions->'semantic_review'->'geography_checked' = 'true'::jsonb,false)
   AND coalesce(resolutions->'semantic_review'->'contradictions_checked' = 'true'::jsonb,false)
   AND coalesce(resolutions->'semantic_review'->'coverage_checked' = 'true'::jsonb,false)
   AND CASE WHEN jsonb_typeof(resolutions->'semantic_review'->'claim_assessments')='array'
       THEN jsonb_array_length(resolutions->'semantic_review'->'claim_assessments')>0 ELSE false END
   AND coalesce(resolutions->'semantic_review'->'unresolved_issues' = '[]'::jsonb,false)
   AND coalesce(resolutions->'validation_report'->>'validator_version' = '3.0',false)
   AND coalesce(resolutions->'validation_report'->>'schema_status' = 'passed',false)
   AND coalesce(resolutions->'validation_report'->>'provenance_status' = 'passed',false)
 )
) NOT VALID;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('018_research_semantic_review.sql','7908e13c45388eb5462b70c9412a30a587b877b5e70b2379ffe188c8c84174d6','supabase','cdb36ebd33b2b2a3d1bd7ab145fe0cb0ac347c760d1096f3eecdc978a75bc5f2');
-- Source: 019_planning_storage.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

ALTER TABLE mi.jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE mi.jobs ADD CONSTRAINT jobs_kind_check CHECK(kind IN('website_crawl','research','strategy','strategy_critique','thirty_day_plan','creative_brief'));
ALTER TABLE mi.workflow_instances ADD COLUMN selected_strategy_approval_id uuid;
ALTER TABLE mi.workflow_instances ADD FOREIGN KEY(selected_strategy_approval_id,brand_id) REFERENCES mi.strategy_approvals(id,brand_id);
ALTER TABLE mi.workflow_instances ADD COLUMN approved_plan_id uuid;
ALTER TABLE mi.workflow_instances ADD FOREIGN KEY(approved_plan_id,brand_id) REFERENCES mi.artifacts(id,brand_id);
ALTER TABLE mi.workflow_instances ADD COLUMN approved_creative_id uuid;
ALTER TABLE mi.workflow_instances ADD FOREIGN KEY(approved_creative_id,brand_id) REFERENCES mi.artifacts(id,brand_id);
CREATE TABLE mi.planning_outputs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,brand_id uuid NOT NULL,
 workflow_id uuid NOT NULL,job_id uuid NOT NULL,dispatch_id uuid NOT NULL UNIQUE,artifact_id uuid NOT NULL UNIQUE,
 stage text NOT NULL CHECK(stage IN('strategy','strategy_critique','thirty_day_plan','creative_brief')),
 profile_artifact_id uuid NOT NULL,research_artifact_id uuid NOT NULL,parents jsonb NOT NULL CHECK(jsonb_typeof(parents)='array'),
 body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'),validation jsonb NOT NULL CHECK(jsonb_typeof(validation)='object'),
 created_by uuid NOT NULL REFERENCES identity.users(id),created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(workflow_id,brand_id) REFERENCES mi.workflow_instances(id,brand_id),
 FOREIGN KEY(job_id,brand_id) REFERENCES mi.jobs(id,brand_id),
 FOREIGN KEY(dispatch_id,brand_id) REFERENCES mi.model_dispatches(id,brand_id),
 FOREIGN KEY(artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id),
 FOREIGN KEY(profile_artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id),
 FOREIGN KEY(research_artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id)
);
CREATE INDEX planning_outputs_brand ON mi.planning_outputs(brand_id,stage,created_at);
CREATE TABLE mi.planning_reviews (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,brand_id uuid NOT NULL,output_id uuid NOT NULL UNIQUE,
 artifact_sha256 text NOT NULL CHECK(length(artifact_sha256)=64),reviewer_id uuid NOT NULL REFERENCES identity.users(id),
 approval_id uuid NOT NULL,decision text NOT NULL CHECK(decision IN('approved','revision_requested','rejected')),
 details jsonb NOT NULL CHECK(jsonb_typeof(details)='object'),created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(output_id,brand_id) REFERENCES mi.planning_outputs(id,brand_id),
 FOREIGN KEY(approval_id,brand_id) REFERENCES mi.approvals(id,brand_id)
);
CREATE INDEX planning_reviews_brand ON mi.planning_reviews(brand_id,output_id);
ALTER TABLE mi.planning_outputs ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.planning_outputs FORCE ROW LEVEL SECURITY;
CREATE POLICY planning_read ON mi.planning_outputs FOR SELECT TO mi_app USING(mi.staff_brand(brand_id) OR mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_write ON mi.planning_outputs FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'planning:run') AND created_by=mi.actor_id());
GRANT SELECT,INSERT ON mi.planning_outputs TO mi_app;
CREATE TRIGGER planning_output_immutable BEFORE UPDATE OR DELETE ON mi.planning_outputs FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
CREATE TRIGGER planning_output_audit AFTER INSERT ON mi.planning_outputs FOR EACH ROW EXECUTE FUNCTION mi.record_change();
ALTER TABLE mi.planning_reviews ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.planning_reviews FORCE ROW LEVEL SECURITY;
CREATE POLICY planning_review_read ON mi.planning_reviews FOR SELECT TO mi_app USING(mi.staff_brand(brand_id) OR mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_review_write ON mi.planning_reviews FOR INSERT TO mi_app WITH CHECK(mi.can_org_admin(organization_id) AND reviewer_id=mi.actor_id());
GRANT SELECT,INSERT ON mi.planning_reviews TO mi_app;
CREATE TRIGGER planning_review_immutable BEFORE UPDATE OR DELETE ON mi.planning_reviews FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation();
CREATE TRIGGER planning_review_audit AFTER INSERT ON mi.planning_reviews FOR EACH ROW EXECUTE FUNCTION mi.record_change();
CREATE FUNCTION mi.planning_owners(bid uuid) RETURNS SETOF uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 SELECT u.id FROM identity.users u JOIN mi.organization_memberships om ON om.user_id=u.id
 JOIN mi.brand_workspaces b ON b.organization_id=om.organization_id
 WHERE b.id=bid AND NOT u.disabled AND NOT u.is_service AND om.role IN('founder','admin','team')
 AND (om.role IN('founder','admin') OR EXISTS(SELECT 1 FROM mi.brand_memberships bm WHERE bm.brand_id=bid AND bm.user_id=u.id))
 AND (mi.staff_brand(bid) OR mi.has_brand_scope(bid,'planning:run')) $$;
REVOKE ALL ON FUNCTION mi.planning_owners(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.planning_owners(uuid) TO mi_app;
ALTER POLICY scoped_write ON mi.jobs WITH CHECK((kind='website_crawl' AND mi.has_brand_scope(brand_id,'crawl:run')) OR (kind='research' AND mi.has_brand_scope(brand_id,'research:run')) OR (kind IN('strategy','strategy_critique','thirty_day_plan','creative_brief') AND mi.has_brand_scope(brand_id,'planning:run')));
ALTER POLICY scoped_update ON mi.jobs USING((kind='website_crawl' AND mi.has_brand_scope(brand_id,'crawl:run')) OR (kind='research' AND mi.has_brand_scope(brand_id,'research:run')) OR (kind IN('strategy','strategy_critique','thirty_day_plan','creative_brief') AND mi.has_brand_scope(brand_id,'planning:run'))) WITH CHECK((kind='website_crawl' AND mi.has_brand_scope(brand_id,'crawl:run')) OR (kind='research' AND mi.has_brand_scope(brand_id,'research:run')) OR (kind IN('strategy','strategy_critique','thirty_day_plan','creative_brief') AND mi.has_brand_scope(brand_id,'planning:run')));
ALTER POLICY scoped_update ON mi.workflow_instances USING(mi.has_brand_scope(brand_id,'crawl:run') OR mi.has_brand_scope(brand_id,'research:run') OR mi.has_brand_scope(brand_id,'planning:run')) WITH CHECK(mi.has_brand_scope(brand_id,'crawl:run') OR mi.has_brand_scope(brand_id,'research:run') OR mi.has_brand_scope(brand_id,'planning:run'));
ALTER POLICY scoped_write ON mi.idempotency_keys WITH CHECK(actor_id=mi.actor_id() AND (mi.has_brand_scope(brand_id,'crawl:run') OR mi.has_brand_scope(brand_id,'research:run') OR mi.has_brand_scope(brand_id,'planning:run')));
ALTER POLICY calls_write ON mi.tool_calls WITH CHECK(actor_id=mi.actor_id() AND (mi.has_brand_scope(brand_id,'crawl:run') OR mi.has_brand_scope(brand_id,'research:run') OR mi.has_brand_scope(brand_id,'planning:run')));
CREATE POLICY planning_artifact_read ON mi.artifacts FOR SELECT TO mi_app USING(mi.has_brand_scope(brand_id,'planning:run') AND kind IN('brand_profile','research_dossier','strategy_options','red_team_review','campaign_plan','creative_brief'));
CREATE POLICY planning_research_read ON mi.research_dossiers FOR SELECT TO mi_app USING(mi.has_brand_scope(brand_id,'planning:run') AND EXISTS(SELECT 1 FROM mi.workflow_instances w WHERE w.brand_id=research_dossiers.brand_id AND w.approved_research_id=research_dossiers.artifact_id));
CREATE POLICY planning_research_review_read ON mi.research_reviews FOR SELECT TO mi_app USING(mi.has_brand_scope(brand_id,'planning:run') AND EXISTS(SELECT 1 FROM mi.workflow_instances w WHERE w.brand_id=research_reviews.brand_id AND w.approved_research_id=research_reviews.artifact_id));
CREATE POLICY planning_template_read ON mi.prompt_templates FOR SELECT TO mi_app USING(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_approval_read ON mi.approvals FOR SELECT TO mi_app USING(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_read ON mi.strategy_options FOR SELECT TO mi_app USING(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_write ON mi.strategy_options FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_read ON mi.red_team_reviews FOR SELECT TO mi_app USING(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_write ON mi.red_team_reviews FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_read ON mi.strategy_approvals FOR SELECT TO mi_app USING(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_read ON mi.campaign_plans FOR SELECT TO mi_app USING(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_write ON mi.campaign_plans FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_read ON mi.calendar_tasks FOR SELECT TO mi_app USING(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_write ON mi.calendar_tasks FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_read ON mi.task_dependencies FOR SELECT TO mi_app USING(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_write ON mi.task_dependencies FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_read ON mi.creative_briefs FOR SELECT TO mi_app USING(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_write ON mi.creative_briefs FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_read ON mi.prompt_runs FOR SELECT TO mi_app USING(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_write ON mi.prompt_runs FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_read ON mi.agent_runs FOR SELECT TO mi_app USING(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_write ON mi.agent_runs FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_read ON mi.agent_outputs FOR SELECT TO mi_app USING(mi.has_brand_scope(brand_id,'planning:run'));
CREATE POLICY planning_write ON mi.agent_outputs FOR INSERT TO mi_app WITH CHECK(mi.has_brand_scope(brand_id,'planning:run'));

-- Strategy selections are human-only even if a legacy broad workflow scope exists.
ALTER POLICY scoped_insert ON mi.strategy_approvals WITH CHECK(mi.can_org_admin(organization_id));
CREATE OR REPLACE FUNCTION mi.validate_plan_approval() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 BEGIN
 IF NOT EXISTS(SELECT 1 FROM mi.strategy_approvals s JOIN mi.approvals a ON a.id=s.approval_id AND a.brand_id=s.brand_id
 WHERE s.id=NEW.strategy_approval_id AND s.brand_id=NEW.brand_id AND a.artifact_id=s.strategy_artifact_id AND a.decision='approved') THEN
 RAISE EXCEPTION 'matching selected strategy approval required' USING ERRCODE='22023'; END IF;
 RETURN NEW; END $$;
CREATE OR REPLACE FUNCTION mi.validate_dispatch_insert() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 BEGIN
 IF NOT EXISTS(SELECT 1 FROM mi.jobs j JOIN mi.workflow_instances w ON w.id=j.workflow_id AND w.brand_id=j.brand_id
 WHERE j.id=NEW.job_id AND j.brand_id=NEW.brand_id AND j.status='running'
 AND j.lease_id=NEW.lease_id AND j.lease_expires_at>now() AND w.id=NEW.workflow_id AND w.approved_profile_id=NEW.profile_artifact_id
 AND ((j.kind='research' AND w.state='researching' AND mi.has_brand_scope(j.brand_id,'research:run')) OR
 (mi.has_brand_scope(j.brand_id,'planning:run') AND ((j.kind='strategy' AND w.state='strategizing') OR
 (j.kind='strategy_critique' AND w.state='reviewing_strategy') OR (j.kind='thirty_day_plan' AND w.state='planning') OR (j.kind='creative_brief' AND w.state='creating_briefs'))))) THEN
 RAISE EXCEPTION 'current authorized model lease and profile required' USING ERRCODE='22023'; END IF;
 RETURN NEW; END $$;
CREATE FUNCTION mi.guard_planning_output() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 BEGIN
 IF NOT EXISTS(SELECT 1 FROM mi.jobs j JOIN mi.workflow_instances w ON w.id=j.workflow_id AND w.brand_id=j.brand_id
 JOIN mi.model_dispatches d ON d.job_id=j.id AND d.brand_id=j.brand_id
 WHERE j.id=NEW.job_id AND j.brand_id=NEW.brand_id AND j.kind=NEW.stage AND j.status='running'
 AND j.lease_id=d.lease_id AND j.lease_expires_at>now() AND d.id=NEW.dispatch_id AND d.state='settled'
 AND w.id=NEW.workflow_id AND w.approved_profile_id=NEW.profile_artifact_id AND w.approved_research_id=NEW.research_artifact_id
 AND j.payload->'parents'=NEW.parents) OR NEW.validation->'valid' IS DISTINCT FROM 'true'::jsonb THEN
 RAISE EXCEPTION 'current validated planning output and lease required' USING ERRCODE='22023'; END IF;
 RETURN NEW; END $$;
CREATE TRIGGER planning_output_guard BEFORE INSERT ON mi.planning_outputs FOR EACH ROW EXECUTE FUNCTION mi.guard_planning_output();
CREATE FUNCTION mi.guard_planning_review() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 BEGIN
 IF NOT mi.can_org_admin(NEW.organization_id) OR NEW.reviewer_id IS DISTINCT FROM mi.actor_id() OR NOT EXISTS(
 SELECT 1 FROM mi.planning_outputs o JOIN mi.artifacts a ON a.id=o.artifact_id AND a.brand_id=o.brand_id
 JOIN mi.workflow_instances w ON w.id=o.workflow_id AND w.brand_id=o.brand_id JOIN mi.approvals ap ON ap.id=NEW.approval_id AND ap.brand_id=o.brand_id
 WHERE o.id=NEW.output_id AND o.brand_id=NEW.brand_id AND a.content_sha256=NEW.artifact_sha256 AND ap.artifact_id=a.id AND ap.decision=NEW.decision
 AND w.approved_profile_id=o.profile_artifact_id AND w.approved_research_id=o.research_artifact_id
 AND o.id=(SELECT p.id FROM mi.planning_outputs p WHERE p.brand_id=o.brand_id AND p.workflow_id=w.id AND p.stage=o.stage ORDER BY p.created_at DESC,p.id DESC LIMIT 1)
 AND ((o.stage='strategy' AND w.state='awaiting_strategy_selection') OR (o.stage='thirty_day_plan' AND w.state='awaiting_plan_approval') OR (o.stage='creative_brief' AND w.state='awaiting_creative_approval'))) THEN
 RAISE EXCEPTION 'current human planning review required' USING ERRCODE='22023'; END IF;
 IF NEW.decision='approved' AND (NEW.details->'evidence_checked' IS DISTINCT FROM 'true'::jsonb OR NEW.details->'dependencies_checked' IS DISTINCT FROM 'true'::jsonb) THEN
 RAISE EXCEPTION 'human evidence and dependency checks required' USING ERRCODE='22023'; END IF;
 RETURN NEW; END $$;
CREATE TRIGGER planning_review_guard BEFORE INSERT ON mi.planning_reviews FOR EACH ROW EXECUTE FUNCTION mi.guard_planning_review();
REVOKE ALL ON FUNCTION mi.guard_planning_output(),mi.guard_planning_review() FROM PUBLIC;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('019_planning_storage.sql','92a310130feaa05fe99390032673c9afae8bbeb0fa0ae6d174d045ea1072ea61','supabase','d2cf2c410176309e1dac0b1d0ee110b184befc9d70614deddb07d816c1c08fd7');
-- Source: 020_planning_workflow.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

ALTER TABLE mi.workflow_instances DROP CONSTRAINT valid_workflow_state;
ALTER TABLE mi.workflow_instances ADD CONSTRAINT valid_workflow_state CHECK(state IN ('awaiting_creative_approval','awaiting_deliverable_approval','awaiting_inputs','awaiting_onboarding','awaiting_plan_approval','awaiting_profile_confirmation','awaiting_research_confirmation','awaiting_strategy_selection','brand_created','cancelled','completed','crawl_failed','crawl_queued','crawling','creating_briefs','creative_queued','failed','needs_human_review','plan_queued','planning','preparing_deliverables','ready_for_delivery','ready_for_research','ready_for_strategy','red_team_queued','report_queued','research_queued','researching','reviewing_strategy','strategizing','strategy_queued'));
DELETE FROM mi.workflow_transition_rules WHERE from_state='awaiting_creative_approval' AND event='approve_creative';
INSERT INTO mi.workflow_transition_rules VALUES('researching','provider_outcome_unknown','needs_human_review') ON CONFLICT DO NOTHING;
INSERT INTO mi.workflow_transition_rules VALUES('researching','budget_exhausted','needs_human_review') ON CONFLICT DO NOTHING;
INSERT INTO mi.workflow_transition_rules VALUES('ready_for_strategy','queue_strategy','strategy_queued') ON CONFLICT DO NOTHING;
INSERT INTO mi.workflow_transition_rules VALUES('strategy_queued','queue_strategy','strategy_queued') ON CONFLICT DO NOTHING;
INSERT INTO mi.workflow_transition_rules VALUES('red_team_queued','queue_red_team','red_team_queued') ON CONFLICT DO NOTHING;
INSERT INTO mi.workflow_transition_rules VALUES('plan_queued','queue_plan','plan_queued') ON CONFLICT DO NOTHING;
INSERT INTO mi.workflow_transition_rules VALUES('creative_queued','queue_creative','creative_queued') ON CONFLICT DO NOTHING;
INSERT INTO mi.workflow_transition_rules VALUES('awaiting_creative_approval','approve_creative','ready_for_delivery') ON CONFLICT DO NOTHING;
INSERT INTO mi.workflow_transition_rules VALUES('strategizing','provider_outcome_unknown','needs_human_review') ON CONFLICT DO NOTHING;
INSERT INTO mi.workflow_transition_rules VALUES('strategizing','budget_exhausted','needs_human_review') ON CONFLICT DO NOTHING;
INSERT INTO mi.workflow_transition_rules VALUES('reviewing_strategy','provider_outcome_unknown','needs_human_review') ON CONFLICT DO NOTHING;
INSERT INTO mi.workflow_transition_rules VALUES('reviewing_strategy','budget_exhausted','needs_human_review') ON CONFLICT DO NOTHING;
INSERT INTO mi.workflow_transition_rules VALUES('planning','provider_outcome_unknown','needs_human_review') ON CONFLICT DO NOTHING;
INSERT INTO mi.workflow_transition_rules VALUES('planning','budget_exhausted','needs_human_review') ON CONFLICT DO NOTHING;
INSERT INTO mi.workflow_transition_rules VALUES('creating_briefs','provider_outcome_unknown','needs_human_review') ON CONFLICT DO NOTHING;
INSERT INTO mi.workflow_transition_rules VALUES('creating_briefs','budget_exhausted','needs_human_review') ON CONFLICT DO NOTHING;
INSERT INTO mi.workflow_transition_rules VALUES('ready_for_delivery','cancel','cancelled') ON CONFLICT DO NOTHING;
CREATE OR REPLACE FUNCTION mi.guard_workflow_transition() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 DECLARE event_name text; actual_status text; critical boolean;
 BEGIN
 event_name:=current_setting('app.workflow_event',true);
 IF event_name='research_budget' THEN
 IF NOT (mi.has_brand_scope(OLD.brand_id,'research:run') OR mi.has_brand_scope(OLD.brand_id,'planning:run')) OR
 (to_jsonb(NEW)-ARRAY['agent_calls','tokens_used','tokens_reserved','cost_used_paise','cost_reserved_paise','budget_breached','pricing_complete']) IS DISTINCT FROM
 (to_jsonb(OLD)-ARRAY['agent_calls','tokens_used','tokens_reserved','cost_used_paise','cost_reserved_paise','budget_breached','pricing_complete']) OR
 NEW.agent_calls<OLD.agent_calls OR NEW.agent_calls>OLD.agent_calls+1 OR NEW.tokens_used<OLD.tokens_used OR NEW.cost_used_paise<OLD.cost_used_paise OR
 (OLD.budget_breached AND NOT NEW.budget_breached) OR (NOT OLD.pricing_complete AND NEW.pricing_complete) THEN
 RAISE EXCEPTION 'invalid budget mutation' USING ERRCODE='42501'; END IF;
 RETURN NEW;
 END IF;

 IF NEW.brand_id<>OLD.brand_id OR NEW.organization_id<>OLD.organization_id OR NEW.version<>OLD.version+1 THEN
 RAISE EXCEPTION 'immutable workflow identity and monotonic version required' USING ERRCODE='22023'; END IF;
 IF event_name NOT IN ('queue_crawl','start_crawl','crawl_succeeded','crawl_failed','submit_onboarding','confirm_profile','retry','cancel','validation_failed','resume_after_review','queue_research','start_research','research_succeeded','request_inputs','run_failed','provider_outcome_unknown','budget_exhausted','confirm_research','request_research_revision','inputs_provided','queue_strategy','queue_red_team','queue_plan','queue_creative','start_strategy','strategy_succeeded','start_red_team','critique_succeeded','request_strategy_revision','approve_strategy','start_plan','plan_succeeded','request_plan_revision','approve_plan','start_creative','creative_succeeded','request_creative_revision','approve_creative') THEN
 RAISE EXCEPTION 'workflow stage not enabled in current build' USING ERRCODE='42501'; END IF;
 IF NOT EXISTS(SELECT 1 FROM mi.workflow_transition_rules WHERE from_state=OLD.state AND event=event_name AND to_state=NEW.state) THEN
 RAISE EXCEPTION 'invalid workflow transition' USING ERRCODE='22023'; END IF;
 IF event_name IN('confirm_profile','cancel','resume_after_review','retry','confirm_research','request_research_revision','inputs_provided') AND NOT mi.can_org_admin(NEW.organization_id) THEN
 RAISE EXCEPTION 'agency administrator required' USING ERRCODE='42501'; END IF;
 IF event_name='validation_failed' AND (NEW.validation_failures<>least(2,OLD.validation_failures+1) OR
 (OLD.validation_failures>=1 AND NEW.state<>'needs_human_review') OR (OLD.validation_failures=0 AND NEW.state<>OLD.state)) THEN
 RAISE EXCEPTION 'validation failure threshold violated' USING ERRCODE='22023'; END IF;
 IF event_name='submit_onboarding' AND NOT EXISTS(SELECT 1 FROM mi.onboarding_responses WHERE brand_id=NEW.brand_id AND respondent_id=mi.actor_id() AND status='submitted') THEN
 RAISE EXCEPTION 'submitted onboarding required' USING ERRCODE='22023'; END IF;
 IF event_name='confirm_profile' THEN
 SELECT p.status,EXISTS(SELECT 1 FROM jsonb_array_elements(p.profile->'missing_inputs') x WHERE x->>'critical'='true')
 INTO actual_status,critical FROM mi.brand_profiles p WHERE p.brand_id=NEW.brand_id AND p.artifact_id=NEW.approved_profile_id
 AND p.version=(SELECT max(version) FROM mi.brand_profiles WHERE brand_id=NEW.brand_id);
 IF actual_status IS DISTINCT FROM 'confirmed' OR critical IS DISTINCT FROM false OR NOT EXISTS(
 SELECT 1 FROM mi.approvals WHERE brand_id=NEW.brand_id AND artifact_id=NEW.approved_profile_id AND decision='approved') THEN
 RAISE EXCEPTION 'current confirmed profile and human approval without critical gaps required' USING ERRCODE='22023'; END IF;
 END IF;

 IF event_name IN('queue_research','start_research') AND (NEW.approved_profile_id IS NULL OR NOT EXISTS(
 SELECT 1 FROM mi.brand_profiles p WHERE p.brand_id=NEW.brand_id AND p.artifact_id=NEW.approved_profile_id AND p.status='confirmed'
 AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(p.profile->'missing_inputs') x WHERE x->>'critical'='true'))) THEN
 RAISE EXCEPTION 'confirmed intake required' USING ERRCODE='22023'; END IF;
 IF event_name='confirm_research' AND NOT EXISTS(
 SELECT 1 FROM mi.research_reviews r JOIN mi.research_dossiers d ON d.id=r.dossier_id AND d.brand_id=r.brand_id
 JOIN mi.artifacts a ON a.id=d.artifact_id AND a.brand_id=d.brand_id
 WHERE r.brand_id=NEW.brand_id AND r.artifact_id=NEW.approved_research_id AND r.reviewer_id=mi.actor_id()
 AND r.decision='approved' AND r.artifact_sha256=a.content_sha256 AND d.profile_artifact_id=NEW.approved_profile_id) THEN
 RAISE EXCEPTION 'matching human research review required' USING ERRCODE='22023'; END IF;
 IF NEW.approved_research_id IS DISTINCT FROM OLD.approved_research_id AND event_name<>'confirm_research' THEN
 RAISE EXCEPTION 'research approval cannot be forged' USING ERRCODE='42501'; END IF;
 IF NEW.approved_profile_id IS DISTINCT FROM OLD.approved_profile_id AND event_name<>'confirm_profile' THEN
 RAISE EXCEPTION 'profile approval cannot be forged' USING ERRCODE='42501'; END IF;
 RETURN NEW;
 END $$;

CREATE FUNCTION mi.guard_phase4_workflow() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 DECLARE ev text; target_stage text; target_artifact uuid;
 BEGIN
 ev:=current_setting('app.workflow_event',true);
 IF NEW.approved_strategy_id IS DISTINCT FROM OLD.approved_strategy_id AND ev<>'approve_strategy' OR
 NEW.selected_strategy_approval_id IS DISTINCT FROM OLD.selected_strategy_approval_id AND ev<>'approve_strategy' OR
 NEW.approved_plan_id IS DISTINCT FROM OLD.approved_plan_id AND ev<>'approve_plan' OR
 NEW.approved_creative_id IS DISTINCT FROM OLD.approved_creative_id AND ev<>'approve_creative' THEN
 RAISE EXCEPTION 'human planning approvals cannot be forged' USING ERRCODE='42501'; END IF;
 IF ev IN('approve_strategy','approve_plan','approve_creative','request_strategy_revision','request_plan_revision','request_creative_revision') AND NOT mi.can_org_admin(NEW.organization_id) THEN
 RAISE EXCEPTION 'agency administrator review required' USING ERRCODE='42501'; END IF;
 IF OLD.state IN('strategy_queued','strategizing','red_team_queued','reviewing_strategy','awaiting_strategy_selection','plan_queued','planning','awaiting_plan_approval','creative_queued','creating_briefs','awaiting_creative_approval','ready_for_delivery') OR ev='queue_strategy' THEN
 IF NOT mi.has_brand_scope(NEW.brand_id,'planning:run') THEN RAISE EXCEPTION 'planning scope required' USING ERRCODE='42501'; END IF;
 IF ev NOT IN('cancel','research_budget') AND NOT EXISTS(
 SELECT 1 FROM mi.research_dossiers d JOIN mi.research_reviews r ON r.dossier_id=d.id AND r.brand_id=d.brand_id
 JOIN mi.brand_profiles p ON p.artifact_id=d.profile_artifact_id AND p.brand_id=d.brand_id
 WHERE d.artifact_id=NEW.approved_research_id AND d.brand_id=NEW.brand_id AND d.profile_artifact_id=NEW.approved_profile_id
 AND p.status='confirmed' AND p.version=(SELECT max(version) FROM mi.brand_profiles WHERE brand_id=NEW.brand_id)
 AND r.decision='approved' AND r.resolutions->'semantic_review' IS NOT NULL
 AND r.resolutions->'validation_report'->>'validator_version'='3.0'
 AND r.resolutions->'validation_report'->>'provenance_status'='passed') THEN
 RAISE EXCEPTION 'current human-validated research required' USING ERRCODE='22023'; END IF;
 END IF;
 IF ev IN('approve_strategy','approve_plan','approve_creative') THEN
 target_stage:=CASE ev WHEN 'approve_strategy' THEN 'strategy' WHEN 'approve_plan' THEN 'thirty_day_plan' ELSE 'creative_brief' END;
 target_artifact:=CASE ev WHEN 'approve_strategy' THEN NEW.approved_strategy_id WHEN 'approve_plan' THEN NEW.approved_plan_id ELSE NEW.approved_creative_id END;
 IF NOT EXISTS(SELECT 1 FROM mi.planning_reviews r JOIN mi.planning_outputs o ON o.id=r.output_id AND o.brand_id=r.brand_id
 WHERE o.artifact_id=target_artifact AND o.stage=target_stage AND o.brand_id=NEW.brand_id AND r.decision='approved'
 AND r.reviewer_id=mi.actor_id() AND o.profile_artifact_id=NEW.approved_profile_id AND o.research_artifact_id=NEW.approved_research_id) THEN
 RAISE EXCEPTION 'matching human phase 4 review required' USING ERRCODE='22023'; END IF;
 END IF;
 IF ev='approve_strategy' AND NOT EXISTS(SELECT 1 FROM mi.strategy_approvals s JOIN mi.planning_reviews r ON r.approval_id=s.approval_id AND r.brand_id=s.brand_id
 WHERE s.id=NEW.selected_strategy_approval_id AND s.brand_id=NEW.brand_id AND s.strategy_artifact_id=NEW.approved_strategy_id AND r.reviewer_id=mi.actor_id()) THEN
 RAISE EXCEPTION 'exact selected strategy and KPI approval required' USING ERRCODE='22023'; END IF;
 RETURN NEW; END $$;
CREATE TRIGGER phase4_workflow_guard BEFORE UPDATE ON mi.workflow_instances FOR EACH ROW EXECUTE FUNCTION mi.guard_phase4_workflow();
CREATE FUNCTION mi.guard_phase4_review_checks() RETURNS trigger LANGUAGE plpgsql AS $$
 DECLARE st text;
 BEGIN
 SELECT stage INTO st FROM mi.planning_outputs WHERE id=NEW.output_id AND brand_id=NEW.brand_id;
 IF NEW.decision='approved' AND ((st='strategy' AND NEW.details->'distinctness_checked' IS DISTINCT FROM 'true'::jsonb) OR
 (st='thirty_day_plan' AND (NEW.details->'owners_and_schedule_checked' IS DISTINCT FROM 'true'::jsonb OR NEW.details->'budget_checked' IS DISTINCT FROM 'true'::jsonb)) OR
 (st='creative_brief' AND NEW.details->'compliance_checked' IS DISTINCT FROM 'true'::jsonb)) THEN
 RAISE EXCEPTION 'stage-specific human assessment required' USING ERRCODE='22023'; END IF;
 RETURN NEW; END $$;
CREATE TRIGGER phase4_review_checks BEFORE INSERT ON mi.planning_reviews FOR EACH ROW EXECUTE FUNCTION mi.guard_phase4_review_checks();
REVOKE ALL ON FUNCTION mi.guard_phase4_workflow(),mi.guard_phase4_review_checks() FROM PUBLIC;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('020_planning_workflow.sql','2bbd2f4aba341bef9d6571137d4f26be467086fde15208604e821d5b8c39f4a7','supabase','7872c31024ccd7d2da12f467a7e28f02780faa82c030c3f61473ce5f461158fb');
-- Source: 021_planning_failure_recovery.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

-- Failure recording must remain possible after a parent becomes stale.
DO $$ DECLARE definition text; BEGIN
 SELECT pg_get_functiondef('mi.guard_phase4_workflow()'::regprocedure) INTO definition;
 IF position($needle$ev NOT IN('cancel','research_budget')$needle$ in definition)=0 THEN
 RAISE EXCEPTION 'Unexpected phase 4 workflow guard'; END IF;
 definition:=replace(definition,$needle$ev NOT IN('cancel','research_budget')$needle$,$replace$ev NOT IN('cancel','research_budget','run_failed','request_inputs','provider_outcome_unknown','budget_exhausted','validation_failed')$replace$);
 EXECUTE definition;
END $$;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('021_planning_failure_recovery.sql','8fa0553f727852acfe53016eb15215662304ebc989aaa8dcad97e801cac44fc3','supabase','9b2ae1b94cdfe8e2136a1c61349d111e8b926688707a87faf3d5859706b542f9');
-- Source: 022_planning_dispatch_isolation.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

CREATE POLICY planning_dispatch_read ON mi.model_dispatches FOR SELECT TO mi_app USING(mi.has_brand_scope(brand_id,'planning:run') AND EXISTS(SELECT 1 FROM mi.jobs j WHERE j.id=model_dispatches.job_id AND j.brand_id=model_dispatches.brand_id AND j.kind IN('strategy','strategy_critique','thirty_day_plan','creative_brief')));
CREATE POLICY planning_dispatch_insert ON mi.model_dispatches FOR INSERT TO mi_app WITH CHECK(created_by=mi.actor_id() AND mi.has_brand_scope(brand_id,'planning:run') AND EXISTS(SELECT 1 FROM mi.jobs j WHERE j.id=model_dispatches.job_id AND j.brand_id=model_dispatches.brand_id AND j.kind IN('strategy','strategy_critique','thirty_day_plan','creative_brief')));
CREATE POLICY planning_dispatch_update ON mi.model_dispatches FOR UPDATE TO mi_app USING(mi.has_brand_scope(brand_id,'planning:run') AND EXISTS(SELECT 1 FROM mi.jobs j WHERE j.id=model_dispatches.job_id AND j.brand_id=model_dispatches.brand_id AND j.kind IN('strategy','strategy_critique','thirty_day_plan','creative_brief'))) WITH CHECK(mi.has_brand_scope(brand_id,'planning:run') AND EXISTS(SELECT 1 FROM mi.jobs j WHERE j.id=model_dispatches.job_id AND j.brand_id=model_dispatches.brand_id AND j.kind IN('strategy','strategy_critique','thirty_day_plan','creative_brief')));
ALTER POLICY dispatch_insert ON mi.model_dispatches WITH CHECK(created_by=mi.actor_id() AND mi.has_brand_scope(brand_id,'research:run') AND EXISTS(SELECT 1 FROM mi.jobs j WHERE j.id=model_dispatches.job_id AND j.brand_id=model_dispatches.brand_id AND j.kind='research'));
ALTER POLICY dispatch_update ON mi.model_dispatches USING(mi.has_brand_scope(brand_id,'research:run') AND EXISTS(SELECT 1 FROM mi.jobs j WHERE j.id=model_dispatches.job_id AND j.brand_id=model_dispatches.brand_id AND j.kind='research')) WITH CHECK(mi.has_brand_scope(brand_id,'research:run') AND EXISTS(SELECT 1 FROM mi.jobs j WHERE j.id=model_dispatches.job_id AND j.brand_id=model_dispatches.brand_id AND j.kind='research'));
ALTER POLICY planning_read ON mi.prompt_runs USING(mi.has_brand_scope(brand_id,'planning:run') AND EXISTS(SELECT 1 FROM mi.model_dispatches d WHERE d.id=prompt_runs.id AND d.brand_id=prompt_runs.brand_id AND d.output_schema IN('strategy-options-v1.1','red-team-review','30-day-plan-v1.1','creative-batch')));
ALTER POLICY planning_read ON mi.agent_runs USING(mi.has_brand_scope(brand_id,'planning:run') AND EXISTS(SELECT 1 FROM mi.model_dispatches d WHERE d.id=agent_runs.prompt_run_id AND d.brand_id=agent_runs.brand_id AND d.output_schema IN('strategy-options-v1.1','red-team-review','30-day-plan-v1.1','creative-batch')));
ALTER POLICY planning_read ON mi.agent_outputs USING(mi.has_brand_scope(brand_id,'planning:run') AND EXISTS(SELECT 1 FROM mi.agent_runs r WHERE r.id=agent_outputs.agent_run_id AND r.brand_id=agent_outputs.brand_id AND r.agent_role IN('strategy_director','red_team_critic','execution_planner','creative_strategist')));
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('022_planning_dispatch_isolation.sql','de92cd9dbd7b4122247983286033104e7bef203db9325fbc20a5449bdfda85c0','supabase','ced124b38536f0572f61dce4f12642e5ca49b39dd9eee423fd0a864e88d7ef76');
-- Source: 023_planning_relationship_guards.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

CREATE OR REPLACE FUNCTION mi.validate_plan_approval() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 BEGIN
 IF NOT EXISTS(SELECT 1 FROM mi.strategy_approvals s JOIN mi.approvals a ON a.id=s.approval_id AND a.brand_id=s.brand_id
 WHERE s.id=NEW.strategy_approval_id AND s.brand_id=NEW.brand_id AND a.artifact_id=s.strategy_artifact_id AND a.decision='approved'
 AND NEW.strategy_artifact_id=s.strategy_artifact_id AND NEW.approval_id=s.approval_id) THEN
 RAISE EXCEPTION 'matching selected strategy and human approval required' USING ERRCODE='22023'; END IF;
 RETURN NEW; END $$;
CREATE FUNCTION mi.guard_planning_task_owner() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
 IF NOT EXISTS(SELECT 1 FROM mi.planning_owners(NEW.brand_id) allowed(id) WHERE allowed.id=NEW.owner_id) THEN
 RAISE EXCEPTION 'active assigned human task owner required' USING ERRCODE='22023'; END IF;
 RETURN NEW; END $$;
CREATE TRIGGER planning_task_owner_guard BEFORE INSERT ON mi.calendar_tasks FOR EACH ROW EXECUTE FUNCTION mi.guard_planning_task_owner();
CREATE FUNCTION mi.guard_task_dependency_plan() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
 IF NOT EXISTS(SELECT 1 FROM mi.calendar_tasks a JOIN mi.calendar_tasks b ON b.brand_id=a.brand_id AND b.plan_id=a.plan_id
 WHERE a.id=NEW.task_id AND b.id=NEW.dependency_id AND a.brand_id=NEW.brand_id AND b.day<a.day) THEN
 RAISE EXCEPTION 'dependency must be in same plan on an earlier day' USING ERRCODE='22023'; END IF;
 RETURN NEW; END $$;
CREATE TRIGGER dependency_plan_guard BEFORE INSERT ON mi.task_dependencies FOR EACH ROW EXECUTE FUNCTION mi.guard_task_dependency_plan();
REVOKE ALL ON FUNCTION mi.guard_planning_task_owner(),mi.guard_task_dependency_plan() FROM PUBLIC;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('023_planning_relationship_guards.sql','edf573a9da88d599a56c7e603165e62b753ec09b6320fb28d572afca66fd0c07','supabase','ddb26f369d279db3545fee42600a2efb3b5725e04a09717114bfd7941a6af671');
-- Source: 024_planning_review_authority.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

-- Reject service/client self-approval before interpreting stage-specific review fields.
DO $$ DECLARE definition text; BEGIN
 SELECT pg_get_functiondef('mi.guard_phase4_review_checks()'::regprocedure) INTO definition;
 definition:=replace(definition,$needle$SELECT stage INTO st FROM mi.planning_outputs$needle$,
 $replacement$IF NOT mi.can_org_admin(NEW.organization_id) OR NEW.reviewer_id IS DISTINCT FROM mi.actor_id() THEN
 RAISE EXCEPTION 'agency administrator review required' USING ERRCODE='42501'; END IF;
 SELECT stage INTO st FROM mi.planning_outputs$replacement$);
 EXECUTE definition;
END $$;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('024_planning_review_authority.sql','8044db2ca15b805036ae990b64cd9fd59d2dc5abd495d82a41be4df72b8dca6f','supabase','a00ea44900e27a208793f5deb1f92e6b9ffff17bb3916a1dd6983a7a60adefa3');
-- Source: 025_cross_stage_usage_settlement.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

-- A late research response may settle its observed charge after planning has begun.
-- The original shared budget guard still restricts every mutable counter/identity field.
DO $$ DECLARE definition text; BEGIN
 SELECT pg_get_functiondef('mi.guard_phase4_workflow()'::regprocedure) INTO definition;
 definition:=replace(definition,$needle$ev:=current_setting('app.workflow_event',true);$needle$,
 $replacement$ev:=current_setting('app.workflow_event',true);
 IF ev='research_budget' THEN RETURN NEW; END IF;$replacement$);
 EXECUTE definition;
END $$;
ALTER POLICY dispatch_read ON mi.model_dispatches USING(mi.staff_brand(brand_id) OR (mi.has_brand_scope(brand_id,'research:run') AND EXISTS(SELECT 1 FROM mi.jobs j WHERE j.id=model_dispatches.job_id AND j.brand_id=model_dispatches.brand_id AND j.kind='research')));
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('025_cross_stage_usage_settlement.sql','f42199ec337c7959cb31817e7b051d109206423b3ed25d88d576d8512d2b92e5','supabase','881b0167f49cff583355afae47aa3fedb5a447a73a08bffb8557bf7423c4bca5');
-- Source: 026_brand_brain_storage.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

ALTER TABLE mi.brand_workspaces ALTER COLUMN website_url DROP NOT NULL;
CREATE FUNCTION mi.brain_read(bid uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT mi.has_brand_scope(bid,'onboarding:read') AND EXISTS(SELECT 1 FROM identity.users WHERE id=mi.actor_id() AND NOT is_service AND NOT disabled) $$;
CREATE FUNCTION mi.brain_write(bid uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT mi.brain_read(bid) AND mi.has_brand_scope(bid,'onboarding:write') $$;
REVOKE ALL ON FUNCTION mi.brain_read(uuid),mi.brain_write(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.brain_read(uuid),mi.brain_write(uuid) TO mi_app,mi_guard;
CREATE TABLE mi.brand_onboarding_sessions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,brand_id uuid NOT NULL,
 version integer NOT NULL DEFAULT 1 CHECK(version>0),draft jsonb NOT NULL CHECK(jsonb_typeof(draft)='object'),
 status text NOT NULL DEFAULT 'draft' CHECK(status IN('draft','submitted')),
 crawl_job_id uuid,research_job_id uuid,created_by uuid NOT NULL REFERENCES identity.users(id),
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(crawl_job_id,brand_id) REFERENCES mi.jobs(id,brand_id),FOREIGN KEY(research_job_id,brand_id) REFERENCES mi.jobs(id,brand_id)
);
CREATE TABLE mi.brand_brain_versions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,brand_id uuid NOT NULL,
 version integer NOT NULL CHECK(version>0),session_id uuid NOT NULL,profile_artifact_id uuid,dossier_id uuid,
 body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'),content_sha256 text NOT NULL CHECK(length(content_sha256)=64),
 created_by uuid NOT NULL REFERENCES identity.users(id),created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(id,brand_id),UNIQUE(brand_id,version),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(session_id,brand_id) REFERENCES mi.brand_onboarding_sessions(id,brand_id),
 FOREIGN KEY(profile_artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id),FOREIGN KEY(dossier_id,brand_id) REFERENCES mi.research_dossiers(id,brand_id)
);
CREATE TABLE mi.brand_brain_reviews (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,brand_id uuid NOT NULL,
 version_id uuid NOT NULL,finding_id uuid NOT NULL,sequence integer NOT NULL CHECK(sequence>0),
 action text NOT NULL CHECK(action IN('accept','edit','reject','request_revision','mark_unknown')),
 text text CHECK(length(text) BETWEEN 1 AND 10000),reason text NOT NULL CHECK(length(reason) BETWEEN 1 AND 3000),
 actor_name text NOT NULL,created_by uuid NOT NULL REFERENCES identity.users(id),created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(id,brand_id),UNIQUE(version_id,finding_id,sequence),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),FOREIGN KEY(version_id,brand_id) REFERENCES mi.brand_brain_versions(id,brand_id)
);
CREATE TABLE mi.brand_brain_threads (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,brand_id uuid NOT NULL,
 version_id uuid NOT NULL,chapter text NOT NULL CHECK(chapter IN('business','audience','positioning','market','presence','objectives')),
 finding_id uuid,created_by uuid NOT NULL REFERENCES identity.users(id),created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),FOREIGN KEY(version_id,brand_id) REFERENCES mi.brand_brain_versions(id,brand_id)
);
CREATE TABLE mi.brand_brain_comments (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,brand_id uuid NOT NULL,thread_id uuid NOT NULL,
 body text NOT NULL CHECK(length(body) BETWEEN 1 AND 3000),actor_name text NOT NULL,
 created_by uuid NOT NULL REFERENCES identity.users(id),created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),FOREIGN KEY(thread_id,brand_id) REFERENCES mi.brand_brain_threads(id,brand_id)
);
CREATE TABLE mi.brand_brain_thread_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,brand_id uuid NOT NULL,thread_id uuid NOT NULL,
 action text NOT NULL CHECK(action IN('resolve','reopen')),actor_name text NOT NULL,
 created_by uuid NOT NULL REFERENCES identity.users(id),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),FOREIGN KEY(thread_id,brand_id) REFERENCES mi.brand_brain_threads(id,brand_id)
);
CREATE TABLE mi.brand_brain_confirmations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,brand_id uuid NOT NULL,version_id uuid NOT NULL,
 review_sequence integer NOT NULL CHECK(review_sequence>=0),approved_chapters text[] NOT NULL CHECK(cardinality(approved_chapters)>0),
 status text NOT NULL CHECK(status IN('partial','confirmed')),snapshot jsonb NOT NULL,content_sha256 text NOT NULL CHECK(length(content_sha256)=64),
 actor_name text NOT NULL,created_by uuid NOT NULL REFERENCES identity.users(id),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(id,brand_id),UNIQUE(version_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),FOREIGN KEY(version_id,brand_id) REFERENCES mi.brand_brain_versions(id,brand_id)
);
CREATE FUNCTION mi.brain_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
 IF TG_OP='INSERT' AND NEW.created_by IS DISTINCT FROM mi.actor_id() THEN RAISE EXCEPTION 'actor mismatch' USING ERRCODE='42501'; END IF;
 IF TG_OP='UPDATE' AND (NEW.id<>OLD.id OR NEW.brand_id<>OLD.brand_id OR NEW.organization_id<>OLD.organization_id OR NEW.created_by<>OLD.created_by OR NEW.version<>OLD.version+1) THEN
 RAISE EXCEPTION 'immutable identity or stale session version' USING ERRCODE='42501'; END IF;
 RETURN NEW;
 END $$;
REVOKE ALL ON FUNCTION mi.brain_identity_guard() FROM PUBLIC;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['brand_onboarding_sessions','brand_brain_versions','brand_brain_reviews','brand_brain_threads','brand_brain_comments','brand_brain_thread_events','brand_brain_confirmations'] LOOP
 EXECUTE format('ALTER TABLE mi.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE mi.%I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('CREATE INDEX %I ON mi.%I(brand_id,created_at)',t||'_tenant',t);
 EXECUTE format('CREATE POLICY brain_read ON mi.%I FOR SELECT TO mi_app USING(mi.brain_read(brand_id))',t);
 EXECUTE format('CREATE POLICY brain_write ON mi.%I FOR INSERT TO mi_app WITH CHECK(mi.brain_write(brand_id) AND created_by=mi.actor_id())',t);
 EXECUTE format('GRANT SELECT,INSERT ON mi.%I TO mi_app',t);
 EXECUTE format('CREATE TRIGGER identity_guard BEFORE INSERT ON mi.%I FOR EACH ROW EXECUTE FUNCTION mi.brain_identity_guard()',t);
 EXECUTE format('CREATE TRIGGER brain_audit AFTER INSERT ON mi.%I FOR EACH ROW EXECUTE FUNCTION mi.record_change()',t);
 IF t<>'brand_onboarding_sessions' THEN
 EXECUTE format('CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON mi.%I FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation()',t);
 ELSE
 EXECUTE format('CREATE POLICY brain_update ON mi.%I FOR UPDATE TO mi_app USING(mi.brain_write(brand_id)) WITH CHECK(mi.brain_write(brand_id))',t);
 EXECUTE format('GRANT UPDATE ON mi.%I TO mi_app',t);
 EXECUTE format('CREATE TRIGGER identity_update BEFORE UPDATE ON mi.%I FOR EACH ROW EXECUTE FUNCTION mi.brain_identity_guard()',t);
 EXECUTE format('CREATE TRIGGER brain_update_audit AFTER UPDATE ON mi.%I FOR EACH ROW EXECUTE FUNCTION mi.record_change()',t);
 END IF;
 END LOOP;
 END $$;
DROP POLICY brain_write ON mi.brand_brain_confirmations;
CREATE POLICY brain_confirm ON mi.brand_brain_confirmations FOR INSERT TO mi_app WITH CHECK(mi.can_org_admin(organization_id) AND mi.brain_write(brand_id) AND created_by=mi.actor_id());
-- Sanitized job state for assigned client reviewers; never exposes prompts or worker payloads.
GRANT SELECT ON mi.brand_onboarding_sessions TO mi_guard;
CREATE FUNCTION mi.brain_job_status(bid uuid,jid uuid) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 SELECT jsonb_build_object('id',j.id,'kind',j.kind,'status',j.status,'last_error',j.last_error,'attempts',j.attempts)
 FROM mi.jobs j WHERE j.brand_id=bid AND j.id=jid AND mi.brain_read(bid) AND EXISTS(
 SELECT 1 FROM mi.brand_onboarding_sessions s WHERE s.brand_id=bid AND (s.crawl_job_id=jid OR s.research_job_id=jid)) $$;
ALTER FUNCTION mi.brain_job_status(uuid,uuid) OWNER TO mi_guard;
REVOKE ALL ON FUNCTION mi.brain_job_status(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.brain_job_status(uuid,uuid) TO mi_app;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
CREATE POLICY managed_guard_read ON "mi"."brand_onboarding_sessions" FOR SELECT TO mi_guard USING(true);
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('026_brand_brain_storage.sql','094b156cc8e95d821a883a78879ba37cb35da08b42300d8a3ba5d8a687d8326a','supabase','f0505630f71f1e8aba24f637c4265191a3e4a03d8be4cce1a8c4638e60e73bee');
-- Source: 027_brand_brain_review_guards.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

DROP POLICY brain_write ON mi.brand_brain_versions;
CREATE POLICY brain_version_write ON mi.brand_brain_versions FOR INSERT TO mi_app WITH CHECK(mi.brain_write(brand_id) AND mi.staff_brand(brand_id) AND created_by=mi.actor_id());
CREATE FUNCTION mi.guard_brain_review() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 DECLARE doc jsonb; f jsonb; latest integer; original_provenance text;
 BEGIN
 IF NOT mi.brain_write(NEW.brand_id) OR NEW.created_by<>mi.actor_id() THEN RAISE EXCEPTION 'not authorized' USING ERRCODE='42501'; END IF;
 SELECT body INTO doc FROM mi.brand_brain_versions WHERE id=NEW.version_id AND brand_id=NEW.brand_id;
 SELECT finding INTO f FROM jsonb_array_elements(doc->'chapters') chapter CROSS JOIN LATERAL jsonb_array_elements(chapter->'findings') finding WHERE finding->>'id'=NEW.finding_id::text;
 IF f IS NULL THEN RAISE EXCEPTION 'finding not in version' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM mi.brand_brain_confirmations WHERE version_id=NEW.version_id AND brand_id=NEW.brand_id) THEN RAISE EXCEPTION 'confirmed version immutable' USING ERRCODE='42501'; END IF;
 SELECT coalesce(max(sequence),0) INTO latest FROM mi.brand_brain_reviews WHERE version_id=NEW.version_id AND brand_id=NEW.brand_id AND finding_id=NEW.finding_id;
 IF NEW.sequence<>latest+1 THEN RAISE EXCEPTION 'stale review' USING ERRCODE='23514'; END IF;
 IF NEW.action='accept' AND NOT mi.can_org_admin(NEW.organization_id) THEN RAISE EXCEPTION 'founder/admin confirmation required' USING ERRCODE='42501'; END IF;
 SELECT full_name INTO NEW.actor_name FROM identity.users WHERE id=mi.actor_id();
 RETURN NEW;
 END $$;
REVOKE ALL ON FUNCTION mi.guard_brain_review() FROM PUBLIC;
CREATE TRIGGER review_guard BEFORE INSERT ON mi.brand_brain_reviews FOR EACH ROW EXECUTE FUNCTION mi.guard_brain_review();
CREATE FUNCTION mi.brain_activity(bid uuid) RETURNS TABLE(id uuid,entity_type text,action text,entity_id uuid,actor_id uuid,actor_name text,occurred_at timestamptz)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 SELECT a.id,a.entity_type,a.action,a.entity_id,a.actor_id,u.full_name,a.occurred_at FROM mi.audit_logs a JOIN identity.users u ON u.id=a.actor_id
 WHERE a.brand_id=bid AND mi.brain_read(bid) AND a.entity_type IN('brand_brain_versions','brand_brain_reviews','brand_brain_threads','brand_brain_comments','brand_brain_thread_events','brand_brain_confirmations')
 ORDER BY a.occurred_at DESC LIMIT 100 $$;
GRANT SELECT ON mi.audit_logs TO mi_guard;
ALTER FUNCTION mi.brain_activity(uuid) OWNER TO mi_guard;
REVOKE ALL ON FUNCTION mi.brain_activity(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.brain_activity(uuid) TO mi_app;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
CREATE POLICY managed_guard_read ON "mi"."audit_logs" FOR SELECT TO mi_guard USING(true);
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('027_brand_brain_review_guards.sql','2d956cc062f0ba7e11ae1634e16e82c3d62d2d70f689b40e202adb90d253bf59','supabase','129da9d916ab2f21d3241bab0b1932b16907e516cda3026e37eb7c452a2e5d04');
-- Source: 028_brand_brain_status_access.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

-- Only the sanitized, brand-authorized status function is callable; the API role gains no new job grants.
GRANT SELECT ON mi.jobs TO mi_guard;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
CREATE POLICY managed_guard_read ON "mi"."jobs" FOR SELECT TO mi_guard USING(true);
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('028_brand_brain_status_access.sql','923458a8e6d68543fc38fd03dd7395c769eccde46b070dbca3ee757307c432e3','supabase','d0d6649ec7ddc8c073128a5b0ad4d451432a7fc72420bcd7685b4a0c27ad2b4d');
-- Source: 029_brand_brain_insert_guard.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

CREATE OR REPLACE FUNCTION mi.brain_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
 IF TG_OP='INSERT' THEN
 IF NEW.created_by IS DISTINCT FROM mi.actor_id() THEN RAISE EXCEPTION 'actor mismatch' USING ERRCODE='42501'; END IF;
 ELSIF TG_OP='UPDATE' THEN
 IF NEW.id<>OLD.id OR NEW.brand_id<>OLD.brand_id OR NEW.organization_id<>OLD.organization_id OR NEW.created_by<>OLD.created_by OR NEW.version<>OLD.version+1 THEN
 RAISE EXCEPTION 'immutable identity or stale session version' USING ERRCODE='42501'; END IF;
 END IF;
 RETURN NEW;
 END $$;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('029_brand_brain_insert_guard.sql','9acb9437abea66b6570c26d34347106435041d886a9e67a76f8c8ce2790bf27f','supabase','e6ea001475fe47ac7c57d5c9a4de79d0983a1f18db158866c6c23185c53177e2');
-- Source: 030_brand_brain_social_profile.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

ALTER TABLE mi.brand_profiles DROP CONSTRAINT brand_profiles_check;
ALTER TABLE mi.brand_profiles ADD CHECK(
 profile ? 'brand_id' AND profile->>'brand_id'=brand_id::text AND profile->>'version'=version::text
 AND profile->>'approval_status'=status AND profile->>'schema_version' IN('1.0','1.1'));
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('030_brand_brain_social_profile.sql','50ca4bd0152b275eeca7247d4e12393808147361a7baf2bac9ab132ca3d90fc5','supabase','53b45e0a7c2bdfcc4542e65853ec8efb64db1c1b55b3685f5435e65c5181ffb7');
-- Source: 031_function_search_paths.sql

-- Pin resolution for invoker helpers and triggers in the new private app only.
ALTER FUNCTION mi.can_org_admin(uuid) SET search_path=pg_catalog;
ALTER FUNCTION mi.staff_brand(uuid) SET search_path=pg_catalog;
ALTER FUNCTION mi.reject_mutation() SET search_path=pg_catalog;
ALTER FUNCTION mi.guard_approval() SET search_path=pg_catalog;
ALTER FUNCTION mi.disabled_integration() SET search_path=pg_catalog;
ALTER FUNCTION mi.validate_workflow_initialization() SET search_path=pg_catalog;
ALTER FUNCTION mi.guard_dispatch() SET search_path=pg_catalog;
ALTER FUNCTION mi.guard_phase4_review_checks() SET search_path=pg_catalog;
ALTER FUNCTION mi.guard_planning_task_owner() SET search_path=pg_catalog;
ALTER FUNCTION mi.guard_task_dependency_plan() SET search_path=pg_catalog;
ALTER FUNCTION mi.brain_read(uuid) SET search_path=pg_catalog;
ALTER FUNCTION mi.brain_write(uuid) SET search_path=pg_catalog;
ALTER FUNCTION mi.brain_identity_guard() SET search_path=pg_catalog;

INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('031_function_search_paths.sql','a8b6d1e9b745cd62cba9b1bac21cf34bdfeadfd74f2d7dbc9541bff3808b446d','supabase','1eb01e9e3956d12ba264896dd17344e2dcca9148fbaf455c0689ae4a2488e907');

-- Source: 032_client_access.sql
GRANT CREATE ON SCHEMA mi TO mi_guard;

GRANT CREATE ON SCHEMA identity TO mi_guard;
-- Dedicated client identities are issued by an agency owner, never by public signup.
ALTER TABLE identity.users ADD COLUMN requires_password_change boolean NOT NULL DEFAULT false;
ALTER TABLE identity.users ADD COLUMN managed_by_organization_id uuid REFERENCES mi.organizations(id);
GRANT SELECT(requires_password_change,managed_by_organization_id) ON identity.users TO mi_api;

CREATE TABLE mi.client_accounts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL,
 brand_id uuid NOT NULL, user_id uuid NOT NULL UNIQUE REFERENCES identity.users(id),
 created_by uuid NOT NULL REFERENCES identity.users(id), created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(organization_id,user_id) REFERENCES mi.organization_memberships(organization_id,user_id)
);
CREATE INDEX client_accounts_org ON mi.client_accounts(organization_id,created_at);
CREATE INDEX client_accounts_brand ON mi.client_accounts(brand_id);
ALTER TABLE mi.client_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE mi.client_accounts FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_read ON mi.client_accounts FOR SELECT TO mi_api USING(mi.can_org_admin(organization_id));
CREATE POLICY owner_insert ON mi.client_accounts FOR INSERT TO mi_api WITH CHECK(mi.can_org_admin(organization_id) AND created_by=mi.actor_id());
CREATE POLICY guard_read ON mi.client_accounts FOR SELECT TO mi_guard USING(true);
GRANT SELECT,INSERT ON mi.client_accounts TO mi_api;
GRANT SELECT ON mi.client_accounts TO mi_guard;
CREATE TRIGGER client_account_audit AFTER INSERT ON mi.client_accounts FOR EACH ROW EXECUTE FUNCTION mi.record_change();

-- Fixed, checked functions grant the API no direct password UPDATE authority.
GRANT INSERT ON identity.users TO mi_guard;
GRANT UPDATE(password_hash,requires_password_change,disabled) ON identity.users TO mi_guard;
CREATE POLICY managed_user_insert ON identity.users FOR INSERT TO mi_guard WITH CHECK(NOT is_service AND managed_by_organization_id IS NOT NULL);
CREATE POLICY managed_user_update ON identity.users FOR UPDATE TO mi_guard USING(NOT is_service) WITH CHECK(NOT is_service);
GRANT SELECT,UPDATE ON identity.sessions TO mi_guard;
CREATE POLICY guard_session_read ON identity.sessions FOR SELECT TO mi_guard USING(true);
CREATE POLICY guard_session_revoke ON identity.sessions FOR UPDATE TO mi_guard USING(true) WITH CHECK(true);

CREATE FUNCTION identity.create_client_user(oid uuid,uid uuid,address text,display_name text,encoded_password text) RETURNS uuid
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 BEGIN
 IF NOT mi.can_org_admin(oid) OR mi.is_bound_service() THEN RAISE EXCEPTION 'Owner access required' USING ERRCODE='42501'; END IF;
 IF encoded_password NOT LIKE '$argon2id$%' THEN RAISE EXCEPTION 'Password hash required' USING ERRCODE='22023'; END IF;
 INSERT INTO identity.users(id,email,full_name,password_hash,requires_password_change,managed_by_organization_id)
 VALUES(uid,lower(address),display_name,encoded_password,true,oid);
 RETURN uid;
 END $$;
ALTER FUNCTION identity.create_client_user(uuid,uuid,text,text,text) OWNER TO mi_guard;

CREATE FUNCTION identity.manage_client_user(oid uuid,uid uuid,operation text,encoded_password text DEFAULT NULL) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 BEGIN
 IF NOT mi.can_org_admin(oid) OR mi.is_bound_service() OR uid=mi.actor_id() OR NOT EXISTS(
 SELECT 1 FROM mi.client_accounts a JOIN identity.users u ON u.id=a.user_id
 WHERE a.organization_id=oid AND a.user_id=uid AND u.managed_by_organization_id=oid AND NOT u.is_service)
 THEN RAISE EXCEPTION 'Client account unavailable' USING ERRCODE='42501'; END IF;
 PERFORM 1 FROM identity.users WHERE id=uid FOR UPDATE;
 IF operation='reset_password' AND encoded_password LIKE '$argon2id$%' THEN
 UPDATE identity.users SET password_hash=encoded_password,requires_password_change=true WHERE id=uid;
 ELSIF operation='suspend' THEN UPDATE identity.users SET disabled=true WHERE id=uid;
 ELSIF operation='resume' THEN UPDATE identity.users SET disabled=false WHERE id=uid;
 ELSE RAISE EXCEPTION 'Invalid account operation' USING ERRCODE='22023'; END IF;
 UPDATE identity.sessions SET revoked_at=now() WHERE user_id=uid AND revoked_at IS NULL;
 END $$;
ALTER FUNCTION identity.manage_client_user(uuid,uuid,text,text) OWNER TO mi_guard;

CREATE FUNCTION identity.change_own_password(encoded_password text) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 BEGIN
 IF mi.is_bound_service() OR encoded_password NOT LIKE '$argon2id$%' OR NOT EXISTS(
 SELECT 1 FROM identity.users WHERE id=mi.actor_id() AND NOT disabled AND NOT is_service)
 THEN RAISE EXCEPTION 'Account unavailable' USING ERRCODE='42501'; END IF;
 PERFORM 1 FROM identity.users WHERE id=mi.actor_id() FOR UPDATE;
 UPDATE identity.users SET password_hash=encoded_password,requires_password_change=false WHERE id=mi.actor_id();
 UPDATE identity.sessions SET revoked_at=now() WHERE user_id=mi.actor_id() AND revoked_at IS NULL;
 END $$;
ALTER FUNCTION identity.change_own_password(text) OWNER TO mi_guard;

CREATE FUNCTION mi.client_account_directory(oid uuid) RETURNS jsonb
 LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 DECLARE result jsonb;
 BEGIN
 IF NOT mi.can_org_admin(oid) OR mi.is_bound_service() THEN RAISE EXCEPTION 'Owner access required' USING ERRCODE='42501'; END IF;
 SELECT coalesce(jsonb_agg(jsonb_build_object('id',a.id,'brand_id',a.brand_id,'brand_name',b.name,
 'email',u.email,'full_name',u.full_name,'status',CASE WHEN u.disabled THEN 'suspended' ELSE 'active' END,
 'requires_password_change',u.requires_password_change,'onboarding_status',coalesce(s.status,'not_started'),
 'created_at',a.created_at) ORDER BY a.created_at DESC),'[]'::jsonb) INTO result
 FROM mi.client_accounts a JOIN identity.users u ON u.id=a.user_id JOIN mi.brand_workspaces b ON b.id=a.brand_id
 LEFT JOIN LATERAL(SELECT status FROM mi.brand_onboarding_sessions WHERE brand_id=a.brand_id ORDER BY created_at DESC LIMIT 1) s ON true
 WHERE a.organization_id=oid;
 RETURN result;
 END $$;
ALTER FUNCTION mi.client_account_directory(uuid) OWNER TO mi_guard;

-- A client may submit its own setup without receiving broad workspace UPDATE privileges.
GRANT UPDATE(name,website_url,intake) ON mi.brand_workspaces TO mi_guard;
CREATE POLICY guard_client_setup ON mi.brand_workspaces FOR UPDATE TO mi_guard USING(mi.brain_write(id)) WITH CHECK(mi.brain_write(id));
CREATE FUNCTION mi.submit_client_setup(bid uuid,setup jsonb) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 DECLARE site text;
 BEGIN
 IF mi.is_bound_service() OR NOT mi.brain_write(bid) OR NOT EXISTS(
 SELECT 1 FROM mi.client_accounts WHERE brand_id=bid) THEN RAISE EXCEPTION 'Client workspace unavailable' USING ERRCODE='42501'; END IF;
 IF jsonb_typeof(setup)<>'object' OR length(trim(setup->>'brand_name')) NOT BETWEEN 1 AND 300
 OR coalesce(jsonb_array_length(setup->'online_presence'),0)=0 OR coalesce(jsonb_array_length(setup->'objectives'),0)=0
 OR coalesce(length(trim(setup->>'category')),0)=0 THEN RAISE EXCEPTION 'Complete setup required' USING ERRCODE='22023'; END IF;
 SELECT value->>'url' INTO site FROM jsonb_array_elements(setup->'online_presence') WHERE value->>'platform'='website' LIMIT 1;
 UPDATE mi.brand_workspaces SET name=setup->>'brand_name',website_url=site,
 intake=setup||jsonb_build_object('client_onboarding',true,'awaiting_configuration',true)
 WHERE id=bid;
 END $$;
ALTER FUNCTION mi.submit_client_setup(uuid,jsonb) OWNER TO mi_guard;

REVOKE ALL ON FUNCTION identity.create_client_user(uuid,uuid,text,text,text),identity.manage_client_user(uuid,uuid,text,text),identity.change_own_password(text),mi.client_account_directory(uuid),mi.submit_client_setup(uuid,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity.create_client_user(uuid,uuid,text,text,text),identity.manage_client_user(uuid,uuid,text,text),identity.change_own_password(text),mi.client_account_directory(uuid),mi.submit_client_setup(uuid,jsonb) TO mi_api;
REVOKE CREATE ON SCHEMA identity FROM mi_guard;
;
GRANT USAGE ON SCHEMA "extensions" TO mi_app,mi_guard;
REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard;
CREATE POLICY managed_guard_read ON "identity"."sessions" FOR SELECT TO mi_guard USING(true);
CREATE POLICY managed_guard_read ON "mi"."client_accounts" FOR SELECT TO mi_guard USING(true);
REVOKE CREATE ON SCHEMA mi FROM mi_guard;
INSERT INTO mi_meta.schema_migrations(name,sha256,profile,effective_sha256) VALUES('032_client_access.sql','1ab4c6782ad69eec0d1a9c2ab412f5e640acb3f02c5e668e8b8df57ef5c47f19','supabase','7b04c635376306fff734af154205a26cb93cd5aa7c61512fba459f328ab128e9');
