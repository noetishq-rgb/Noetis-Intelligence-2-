BEGIN;
-- Pin resolution for invoker helpers and triggers in the new private app only.
ALTER FUNCTION mi.can_org_admin(uuid) SET search_path=pg_catalog;
ALTER FUNCTION mi.staff_brand(uuid) SET search_path=pg_catalog;
ALTER FUNCTION mi.reject_mutation() SET search_path=pg_catalog;
ALTER FUNCTION mi.guard_approval() SET search_path=pg_catalog;
ALTER FUNCTION mi.disabled_integration() SET search_path=pg_catalog;
ALTER FUNCTION mi.validate_workflow_initialization() SET search_path=pg_catalog;
ALTER FUNCTION mi.guard_dispatch() SET search_path=pg_catalog;
ALTER FUNCTION mi.guard_phase4_review_checks() SET search_path=pg_catalog;
ALTER FUNCTION mi.guard_planning_task_owner() SET search_path=pg_catalog;
ALTER FUNCTION mi.guard_task_dependency_plan() SET search_path=pg_catalog;
ALTER FUNCTION mi.brain_read(uuid) SET search_path=pg_catalog;
ALTER FUNCTION mi.brain_write(uuid) SET search_path=pg_catalog;
ALTER FUNCTION mi.brain_identity_guard() SET search_path=pg_catalog;
COMMIT;
