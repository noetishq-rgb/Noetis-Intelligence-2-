BEGIN;
-- A late research response may settle its observed charge after planning has begun.
-- The original shared budget guard still restricts every mutable counter/identity field.
DO $$ DECLARE definition text; BEGIN
 SELECT pg_get_functiondef('mi.guard_phase4_workflow()'::regprocedure) INTO definition;
 definition:=replace(definition,$needle$ev:=current_setting('app.workflow_event',true);$needle$,
 $replacement$ev:=current_setting('app.workflow_event',true);
 IF ev='research_budget' THEN RETURN NEW; END IF;$replacement$);
 EXECUTE definition;
END $$;
ALTER POLICY dispatch_read ON mi.model_dispatches USING(mi.staff_brand(brand_id) OR (mi.has_brand_scope(brand_id,'research:run') AND EXISTS(SELECT 1 FROM mi.jobs j WHERE j.id=model_dispatches.job_id AND j.brand_id=model_dispatches.brand_id AND j.kind='research')));
COMMIT;
