BEGIN;
DROP TRIGGER record_workflow_transition ON mi.workflow_instances;
CREATE TRIGGER record_workflow_transition AFTER UPDATE ON mi.workflow_instances FOR EACH ROW
 WHEN (OLD.state IS DISTINCT FROM NEW.state OR OLD.version IS DISTINCT FROM NEW.version)
 EXECUTE FUNCTION mi.record_workflow_transition();
CREATE FUNCTION mi.validate_dispatch_insert() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 BEGIN
 IF NOT EXISTS(SELECT 1 FROM mi.jobs j JOIN mi.workflow_instances w ON w.id=j.workflow_id AND w.brand_id=j.brand_id
 WHERE j.id=NEW.job_id AND j.brand_id=NEW.brand_id AND j.kind='research' AND j.status='running'
 AND j.lease_id=NEW.lease_id AND j.lease_expires_at>now() AND w.id=NEW.workflow_id AND w.state='researching'
 AND w.approved_profile_id=NEW.profile_artifact_id) THEN
 RAISE EXCEPTION 'current research lease and profile required' USING ERRCODE='22023'; END IF;
 RETURN NEW;
 END $$;
CREATE TRIGGER dispatch_insert_guard BEFORE INSERT ON mi.model_dispatches FOR EACH ROW EXECUTE FUNCTION mi.validate_dispatch_insert();
CREATE OR REPLACE FUNCTION mi.guard_dispatch() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
 IF (to_jsonb(NEW)-ARRAY['state','input_tokens','output_tokens','cost_paise','error_code','latency_ms','dispatched_at','finished_at']) IS DISTINCT FROM
 (to_jsonb(OLD)-ARRAY['state','input_tokens','output_tokens','cost_paise','error_code','latency_ms','dispatched_at','finished_at'])
 OR NOT ((OLD.state='reserved' AND NEW.state IN('dispatched','rejected')) OR
 (OLD.state='dispatched' AND NEW.state IN('settled','outcome_unknown','rejected'))) THEN
 RAISE EXCEPTION 'immutable or invalid dispatch transition' USING ERRCODE='42501'; END IF;
 IF NEW.state='settled' AND (NEW.input_tokens IS NULL OR NEW.output_tokens IS NULL OR NEW.finished_at IS NULL) THEN
 RAISE EXCEPTION 'observed usage is required for settlement' USING ERRCODE='22023'; END IF;
 RETURN NEW;
 END $$;
REVOKE ALL ON FUNCTION mi.validate_dispatch_insert(),mi.guard_dispatch() FROM PUBLIC;
COMMIT;
