BEGIN;
CREATE OR REPLACE FUNCTION mi.guard_dispatch() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
 IF (to_jsonb(NEW)-ARRAY['state','input_tokens','output_tokens','cost_paise','error_code','latency_ms','dispatched_at','finished_at']) IS DISTINCT FROM
 (to_jsonb(OLD)-ARRAY['state','input_tokens','output_tokens','cost_paise','error_code','latency_ms','dispatched_at','finished_at'])
 OR NOT ((OLD.state='reserved' AND NEW.state IN('dispatched','rejected')) OR
 (OLD.state='dispatched' AND NEW.state IN('settled','outcome_unknown','rejected')) OR (OLD.state='outcome_unknown' AND NEW.state='settled')) THEN
 RAISE EXCEPTION 'immutable or invalid dispatch transition' USING ERRCODE='42501'; END IF;
 IF NEW.state='settled' AND (NEW.input_tokens IS NULL OR NEW.output_tokens IS NULL OR NEW.finished_at IS NULL) THEN
 RAISE EXCEPTION 'observed usage is required for settlement' USING ERRCODE='22023'; END IF;
 RETURN NEW;
 END $$;
COMMIT;
