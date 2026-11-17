BEGIN;
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

COMMIT;
