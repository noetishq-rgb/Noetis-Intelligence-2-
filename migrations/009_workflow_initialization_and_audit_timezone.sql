BEGIN;
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
COMMIT;
