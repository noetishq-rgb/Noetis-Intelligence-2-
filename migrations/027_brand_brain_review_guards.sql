BEGIN;
DROP POLICY brain_write ON mi.brand_brain_versions;
CREATE POLICY brain_version_write ON mi.brand_brain_versions FOR INSERT TO mi_app WITH CHECK(mi.brain_write(brand_id) AND mi.staff_brand(brand_id) AND created_by=mi.actor_id());
CREATE FUNCTION mi.guard_brain_review() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 DECLARE doc jsonb; f jsonb; latest integer; original_provenance text;
 BEGIN
 IF NOT mi.brain_write(NEW.brand_id) OR NEW.created_by<>mi.actor_id() THEN RAISE EXCEPTION 'not authorized' USING ERRCODE='42501'; END IF;
 SELECT body INTO doc FROM mi.brand_brain_versions WHERE id=NEW.version_id AND brand_id=NEW.brand_id;
 SELECT finding INTO f FROM jsonb_array_elements(doc->'chapters') chapter CROSS JOIN LATERAL jsonb_array_elements(chapter->'findings') finding WHERE finding->>'id'=NEW.finding_id::text;
 IF f IS NULL THEN RAISE EXCEPTION 'finding not in version' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM mi.brand_brain_confirmations WHERE version_id=NEW.version_id AND brand_id=NEW.brand_id) THEN RAISE EXCEPTION 'confirmed version immutable' USING ERRCODE='42501'; END IF;
 SELECT coalesce(max(sequence),0) INTO latest FROM mi.brand_brain_reviews WHERE version_id=NEW.version_id AND brand_id=NEW.brand_id AND finding_id=NEW.finding_id;
 IF NEW.sequence<>latest+1 THEN RAISE EXCEPTION 'stale review' USING ERRCODE='23514'; END IF;
 IF NEW.action='accept' AND NOT mi.can_org_admin(NEW.organization_id) THEN RAISE EXCEPTION 'founder/admin confirmation required' USING ERRCODE='42501'; END IF;
 SELECT full_name INTO NEW.actor_name FROM identity.users WHERE id=mi.actor_id();
 RETURN NEW;
 END $$;
REVOKE ALL ON FUNCTION mi.guard_brain_review() FROM PUBLIC;
CREATE TRIGGER review_guard BEFORE INSERT ON mi.brand_brain_reviews FOR EACH ROW EXECUTE FUNCTION mi.guard_brain_review();
CREATE FUNCTION mi.brain_activity(bid uuid) RETURNS TABLE(id uuid,entity_type text,action text,entity_id uuid,actor_id uuid,actor_name text,occurred_at timestamptz)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 SELECT a.id,a.entity_type,a.action,a.entity_id,a.actor_id,u.full_name,a.occurred_at FROM mi.audit_logs a JOIN identity.users u ON u.id=a.actor_id
 WHERE a.brand_id=bid AND mi.brain_read(bid) AND a.entity_type IN('brand_brain_versions','brand_brain_reviews','brand_brain_threads','brand_brain_comments','brand_brain_thread_events','brand_brain_confirmations')
 ORDER BY a.occurred_at DESC LIMIT 100 $$;
GRANT SELECT ON mi.audit_logs TO mi_guard;
ALTER FUNCTION mi.brain_activity(uuid) OWNER TO mi_guard;
REVOKE ALL ON FUNCTION mi.brain_activity(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.brain_activity(uuid) TO mi_app;
COMMIT;
