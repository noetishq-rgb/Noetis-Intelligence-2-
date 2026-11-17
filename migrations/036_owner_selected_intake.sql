BEGIN;
-- An owner may advance a selected, current client submission for review while
-- preserving the real respondent. This does not approve a profile or research.
DO $patch$
DECLARE definition text;
 needle text := $old$IF event_name='submit_onboarding' AND NOT EXISTS(SELECT 1 FROM mi.onboarding_responses WHERE brand_id=NEW.brand_id AND respondent_id=mi.actor_id() AND status='submitted') THEN$old$;
 replacement text := $new$IF event_name='submit_onboarding' AND NOT EXISTS(
 SELECT 1 FROM mi.onboarding_responses r WHERE r.brand_id=NEW.brand_id AND r.status='submitted'
 AND (r.respondent_id=mi.actor_id() OR (
  mi.can_org_admin(NEW.organization_id) AND NOT mi.is_bound_service()
  AND r.id::text=current_setting('app.onboarding_response_id',true)
  AND r.version=(SELECT max(v.version) FROM mi.onboarding_responses v WHERE v.brand_id=r.brand_id AND v.respondent_id=r.respondent_id)
 ))) THEN$new$;
BEGIN
 SELECT pg_get_functiondef('mi.guard_workflow_transition()'::regprocedure) INTO definition;
 IF position(needle IN definition)=0 THEN RAISE EXCEPTION 'Unexpected workflow submission guard'; END IF;
 EXECUTE replace(definition,needle,replacement);
END $patch$;
COMMIT;
