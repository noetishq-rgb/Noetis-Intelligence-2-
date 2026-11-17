BEGIN;
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
 NEW.entry_hash:=encode(public.digest(convert_to((to_jsonb(NEW)-'entry_hash')::text,'UTF8'),'sha256'),'hex');
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
 CASE WHEN old_data IS NOT NULL THEN encode(public.digest(old_data::text,'sha256'),'hex') END,
 encode(public.digest(row_data::text,'sha256'),'hex'));
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
COMMIT;
