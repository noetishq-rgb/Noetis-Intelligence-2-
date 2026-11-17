BEGIN;
ALTER TABLE mi.artifacts ADD COLUMN content_sha256 text CHECK(content_sha256 IS NULL OR length(content_sha256)=64);
ALTER TABLE mi.approvals ADD COLUMN artifact_sha256 text CHECK(artifact_sha256 IS NULL OR length(artifact_sha256)=64);
ALTER TABLE mi.approvals ADD COLUMN requested_by uuid REFERENCES identity.users(id);
ALTER TABLE mi.approvals ADD COLUMN workflow_id uuid;
ALTER TABLE mi.approvals ADD FOREIGN KEY(workflow_id,brand_id) REFERENCES mi.workflow_instances(id,brand_id);
CREATE FUNCTION mi.bind_approval_target() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 BEGIN
 IF NEW.requested_by IS DISTINCT FROM mi.actor_id() OR NEW.workflow_id IS NULL OR NOT EXISTS(
 SELECT 1 FROM mi.artifacts a WHERE a.id=NEW.artifact_id AND a.brand_id=NEW.brand_id
 AND a.version=NEW.artifact_version AND a.content_sha256 IS NOT NULL AND a.content_sha256=NEW.artifact_sha256) THEN
 RAISE EXCEPTION 'exact artifact hash, workflow and requester required' USING ERRCODE='22023'; END IF;
 RETURN NEW;
 END $$;
CREATE TRIGGER approval_hash_binding BEFORE INSERT ON mi.approvals FOR EACH ROW EXECUTE FUNCTION mi.bind_approval_target();
REVOKE ALL ON FUNCTION mi.bind_approval_target() FROM PUBLIC;
-- Strictly ordered timestamps keep the org chain unambiguous even if clocks move.
CREATE OR REPLACE FUNCTION mi.audit_chain() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi,public AS $$
 DECLARE prior_time timestamptz;
 BEGIN
 IF mi.actor_id() IS NULL THEN RAISE EXCEPTION 'actor context required' USING ERRCODE='42501'; END IF;
 NEW.actor_id:=mi.actor_id();
 NEW.request_id:=coalesce(nullif(current_setting('app.request_id',true),'')::uuid,NEW.request_id);
 PERFORM pg_advisory_xact_lock(hashtextextended(NEW.organization_id::text,0));
 SELECT entry_hash,occurred_at INTO NEW.previous_hash,prior_time FROM mi.audit_logs
 WHERE organization_id=NEW.organization_id ORDER BY occurred_at DESC,id DESC LIMIT 1;
 NEW.occurred_at:=greatest(clock_timestamp(),prior_time+interval '1 microsecond');
 NEW.entry_hash:=encode(public.digest(convert_to((to_jsonb(NEW)-'entry_hash')::text,'UTF8'),'sha256'),'hex');
 RETURN NEW;
 END $$;
COMMIT;
