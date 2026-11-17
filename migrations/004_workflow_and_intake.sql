BEGIN;
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
COMMIT;
