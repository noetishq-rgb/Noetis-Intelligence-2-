BEGIN;
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
COMMIT;
