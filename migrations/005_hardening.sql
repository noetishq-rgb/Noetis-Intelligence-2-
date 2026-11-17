BEGIN;
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
COMMIT;
