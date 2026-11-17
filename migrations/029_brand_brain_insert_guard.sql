BEGIN;
CREATE OR REPLACE FUNCTION mi.brain_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
 IF TG_OP='INSERT' THEN
 IF NEW.created_by IS DISTINCT FROM mi.actor_id() THEN RAISE EXCEPTION 'actor mismatch' USING ERRCODE='42501'; END IF;
 ELSIF TG_OP='UPDATE' THEN
 IF NEW.id<>OLD.id OR NEW.brand_id<>OLD.brand_id OR NEW.organization_id<>OLD.organization_id OR NEW.created_by<>OLD.created_by OR NEW.version<>OLD.version+1 THEN
 RAISE EXCEPTION 'immutable identity or stale session version' USING ERRCODE='42501'; END IF;
 END IF;
 RETURN NEW;
 END $$;
COMMIT;
