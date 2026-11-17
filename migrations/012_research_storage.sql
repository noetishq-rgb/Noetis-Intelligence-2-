BEGIN;
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
COMMIT;
