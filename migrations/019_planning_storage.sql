BEGIN;
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
COMMIT;
