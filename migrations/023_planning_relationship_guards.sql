BEGIN;
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
COMMIT;
