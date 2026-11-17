BEGIN;
-- Failure recording must remain possible after a parent becomes stale.
DO $$ DECLARE definition text; BEGIN
 SELECT pg_get_functiondef('mi.guard_phase4_workflow()'::regprocedure) INTO definition;
 IF position($needle$ev NOT IN('cancel','research_budget')$needle$ in definition)=0 THEN
 RAISE EXCEPTION 'Unexpected phase 4 workflow guard'; END IF;
 definition:=replace(definition,$needle$ev NOT IN('cancel','research_budget')$needle$,$replace$ev NOT IN('cancel','research_budget','run_failed','request_inputs','provider_outcome_unknown','budget_exhausted','validation_failed')$replace$);
 EXECUTE definition;
END $$;
COMMIT;
