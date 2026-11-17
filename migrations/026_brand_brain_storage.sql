BEGIN;
ALTER TABLE mi.brand_workspaces ALTER COLUMN website_url DROP NOT NULL;
CREATE FUNCTION mi.brain_read(bid uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT mi.has_brand_scope(bid,'onboarding:read') AND EXISTS(SELECT 1 FROM identity.users WHERE id=mi.actor_id() AND NOT is_service AND NOT disabled) $$;
CREATE FUNCTION mi.brain_write(bid uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT mi.brain_read(bid) AND mi.has_brand_scope(bid,'onboarding:write') $$;
REVOKE ALL ON FUNCTION mi.brain_read(uuid),mi.brain_write(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.brain_read(uuid),mi.brain_write(uuid) TO mi_app,mi_guard;
CREATE TABLE mi.brand_onboarding_sessions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,brand_id uuid NOT NULL,
 version integer NOT NULL DEFAULT 1 CHECK(version>0),draft jsonb NOT NULL CHECK(jsonb_typeof(draft)='object'),
 status text NOT NULL DEFAULT 'draft' CHECK(status IN('draft','submitted')),
 crawl_job_id uuid,research_job_id uuid,created_by uuid NOT NULL REFERENCES identity.users(id),
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(crawl_job_id,brand_id) REFERENCES mi.jobs(id,brand_id),FOREIGN KEY(research_job_id,brand_id) REFERENCES mi.jobs(id,brand_id)
);
CREATE TABLE mi.brand_brain_versions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,brand_id uuid NOT NULL,
 version integer NOT NULL CHECK(version>0),session_id uuid NOT NULL,profile_artifact_id uuid,dossier_id uuid,
 body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'),content_sha256 text NOT NULL CHECK(length(content_sha256)=64),
 created_by uuid NOT NULL REFERENCES identity.users(id),created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(id,brand_id),UNIQUE(brand_id,version),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(session_id,brand_id) REFERENCES mi.brand_onboarding_sessions(id,brand_id),
 FOREIGN KEY(profile_artifact_id,brand_id) REFERENCES mi.artifacts(id,brand_id),FOREIGN KEY(dossier_id,brand_id) REFERENCES mi.research_dossiers(id,brand_id)
);
CREATE TABLE mi.brand_brain_reviews (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,brand_id uuid NOT NULL,
 version_id uuid NOT NULL,finding_id uuid NOT NULL,sequence integer NOT NULL CHECK(sequence>0),
 action text NOT NULL CHECK(action IN('accept','edit','reject','request_revision','mark_unknown')),
 text text CHECK(length(text) BETWEEN 1 AND 10000),reason text NOT NULL CHECK(length(reason) BETWEEN 1 AND 3000),
 actor_name text NOT NULL,created_by uuid NOT NULL REFERENCES identity.users(id),created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(id,brand_id),UNIQUE(version_id,finding_id,sequence),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),FOREIGN KEY(version_id,brand_id) REFERENCES mi.brand_brain_versions(id,brand_id)
);
CREATE TABLE mi.brand_brain_threads (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,brand_id uuid NOT NULL,
 version_id uuid NOT NULL,chapter text NOT NULL CHECK(chapter IN('business','audience','positioning','market','presence','objectives')),
 finding_id uuid,created_by uuid NOT NULL REFERENCES identity.users(id),created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),FOREIGN KEY(version_id,brand_id) REFERENCES mi.brand_brain_versions(id,brand_id)
);
CREATE TABLE mi.brand_brain_comments (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,brand_id uuid NOT NULL,thread_id uuid NOT NULL,
 body text NOT NULL CHECK(length(body) BETWEEN 1 AND 3000),actor_name text NOT NULL,
 created_by uuid NOT NULL REFERENCES identity.users(id),created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),FOREIGN KEY(thread_id,brand_id) REFERENCES mi.brand_brain_threads(id,brand_id)
);
CREATE TABLE mi.brand_brain_thread_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,brand_id uuid NOT NULL,thread_id uuid NOT NULL,
 action text NOT NULL CHECK(action IN('resolve','reopen')),actor_name text NOT NULL,
 created_by uuid NOT NULL REFERENCES identity.users(id),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(id,brand_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),FOREIGN KEY(thread_id,brand_id) REFERENCES mi.brand_brain_threads(id,brand_id)
);
CREATE TABLE mi.brand_brain_confirmations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,brand_id uuid NOT NULL,version_id uuid NOT NULL,
 review_sequence integer NOT NULL CHECK(review_sequence>=0),approved_chapters text[] NOT NULL CHECK(cardinality(approved_chapters)>0),
 status text NOT NULL CHECK(status IN('partial','confirmed')),snapshot jsonb NOT NULL,content_sha256 text NOT NULL CHECK(length(content_sha256)=64),
 actor_name text NOT NULL,created_by uuid NOT NULL REFERENCES identity.users(id),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(id,brand_id),UNIQUE(version_id),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),FOREIGN KEY(version_id,brand_id) REFERENCES mi.brand_brain_versions(id,brand_id)
);
CREATE FUNCTION mi.brain_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
 IF TG_OP='INSERT' AND NEW.created_by IS DISTINCT FROM mi.actor_id() THEN RAISE EXCEPTION 'actor mismatch' USING ERRCODE='42501'; END IF;
 IF TG_OP='UPDATE' AND (NEW.id<>OLD.id OR NEW.brand_id<>OLD.brand_id OR NEW.organization_id<>OLD.organization_id OR NEW.created_by<>OLD.created_by OR NEW.version<>OLD.version+1) THEN
 RAISE EXCEPTION 'immutable identity or stale session version' USING ERRCODE='42501'; END IF;
 RETURN NEW;
 END $$;
REVOKE ALL ON FUNCTION mi.brain_identity_guard() FROM PUBLIC;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['brand_onboarding_sessions','brand_brain_versions','brand_brain_reviews','brand_brain_threads','brand_brain_comments','brand_brain_thread_events','brand_brain_confirmations'] LOOP
 EXECUTE format('ALTER TABLE mi.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE mi.%I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('CREATE INDEX %I ON mi.%I(brand_id,created_at)',t||'_tenant',t);
 EXECUTE format('CREATE POLICY brain_read ON mi.%I FOR SELECT TO mi_app USING(mi.brain_read(brand_id))',t);
 EXECUTE format('CREATE POLICY brain_write ON mi.%I FOR INSERT TO mi_app WITH CHECK(mi.brain_write(brand_id) AND created_by=mi.actor_id())',t);
 EXECUTE format('GRANT SELECT,INSERT ON mi.%I TO mi_app',t);
 EXECUTE format('CREATE TRIGGER identity_guard BEFORE INSERT ON mi.%I FOR EACH ROW EXECUTE FUNCTION mi.brain_identity_guard()',t);
 EXECUTE format('CREATE TRIGGER brain_audit AFTER INSERT ON mi.%I FOR EACH ROW EXECUTE FUNCTION mi.record_change()',t);
 IF t<>'brand_onboarding_sessions' THEN
 EXECUTE format('CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON mi.%I FOR EACH ROW EXECUTE FUNCTION mi.reject_mutation()',t);
 ELSE
 EXECUTE format('CREATE POLICY brain_update ON mi.%I FOR UPDATE TO mi_app USING(mi.brain_write(brand_id)) WITH CHECK(mi.brain_write(brand_id))',t);
 EXECUTE format('GRANT UPDATE ON mi.%I TO mi_app',t);
 EXECUTE format('CREATE TRIGGER identity_update BEFORE UPDATE ON mi.%I FOR EACH ROW EXECUTE FUNCTION mi.brain_identity_guard()',t);
 EXECUTE format('CREATE TRIGGER brain_update_audit AFTER UPDATE ON mi.%I FOR EACH ROW EXECUTE FUNCTION mi.record_change()',t);
 END IF;
 END LOOP;
 END $$;
DROP POLICY brain_write ON mi.brand_brain_confirmations;
CREATE POLICY brain_confirm ON mi.brand_brain_confirmations FOR INSERT TO mi_app WITH CHECK(mi.can_org_admin(organization_id) AND mi.brain_write(brand_id) AND created_by=mi.actor_id());
-- Sanitized job state for assigned client reviewers; never exposes prompts or worker payloads.
GRANT SELECT ON mi.brand_onboarding_sessions TO mi_guard;
CREATE FUNCTION mi.brain_job_status(bid uuid,jid uuid) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,mi AS $$
 SELECT jsonb_build_object('id',j.id,'kind',j.kind,'status',j.status,'last_error',j.last_error,'attempts',j.attempts)
 FROM mi.jobs j WHERE j.brand_id=bid AND j.id=jid AND mi.brain_read(bid) AND EXISTS(
 SELECT 1 FROM mi.brand_onboarding_sessions s WHERE s.brand_id=bid AND (s.crawl_job_id=jid OR s.research_job_id=jid)) $$;
ALTER FUNCTION mi.brain_job_status(uuid,uuid) OWNER TO mi_guard;
REVOKE ALL ON FUNCTION mi.brain_job_status(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.brain_job_status(uuid,uuid) TO mi_app;
COMMIT;
