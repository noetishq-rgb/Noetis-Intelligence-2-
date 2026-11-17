BEGIN;
ALTER TABLE mi.research_dossiers ADD COLUMN job_id uuid;
ALTER TABLE mi.research_dossiers ADD FOREIGN KEY(job_id,brand_id) REFERENCES mi.jobs(id,brand_id);
CREATE INDEX research_dossiers_job ON mi.research_dossiers(brand_id,job_id);
CREATE FUNCTION mi.guard_research_review() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 BEGIN
 IF NOT mi.can_org_admin(NEW.organization_id) OR NEW.reviewer_id IS DISTINCT FROM mi.actor_id() OR NOT EXISTS(
 SELECT 1 FROM mi.research_dossiers d JOIN mi.artifacts a ON a.id=d.artifact_id AND a.brand_id=d.brand_id
 JOIN mi.workflow_instances w ON w.brand_id=d.brand_id AND w.approved_profile_id=d.profile_artifact_id
 WHERE d.id=NEW.dossier_id AND d.brand_id=NEW.brand_id AND d.artifact_id=NEW.artifact_id
 AND a.content_sha256=NEW.artifact_sha256 AND w.state='awaiting_research_confirmation'
 AND a.version=(SELECT max(version) FROM mi.artifacts WHERE brand_id=NEW.brand_id AND kind='research_dossier')) THEN
 RAISE EXCEPTION 'current human research review target required' USING ERRCODE='22023'; END IF;
 RETURN NEW;
 END $$;
CREATE TRIGGER research_review_target BEFORE INSERT ON mi.research_reviews FOR EACH ROW EXECUTE FUNCTION mi.guard_research_review();
REVOKE ALL ON FUNCTION mi.guard_research_review() FROM PUBLIC;
COMMIT;
