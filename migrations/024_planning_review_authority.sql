BEGIN;
-- Reject service/client self-approval before interpreting stage-specific review fields.
DO $$ DECLARE definition text; BEGIN
 SELECT pg_get_functiondef('mi.guard_phase4_review_checks()'::regprocedure) INTO definition;
 definition:=replace(definition,$needle$SELECT stage INTO st FROM mi.planning_outputs$needle$,
 $replacement$IF NOT mi.can_org_admin(NEW.organization_id) OR NEW.reviewer_id IS DISTINCT FROM mi.actor_id() THEN
 RAISE EXCEPTION 'agency administrator review required' USING ERRCODE='42501'; END IF;
 SELECT stage INTO st FROM mi.planning_outputs$replacement$);
 EXECUTE definition;
END $$;
COMMIT;
