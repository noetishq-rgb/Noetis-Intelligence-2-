BEGIN;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='mi_api') THEN
 CREATE ROLE mi_api NOLOGIN NOBYPASSRLS IN ROLE mi_app;
 END IF;
END $$;
CREATE TABLE mi.service_principals (
 database_role name PRIMARY KEY,
 user_id uuid NOT NULL UNIQUE REFERENCES identity.users(id),
 created_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT ON mi.service_principals TO mi_guard;
CREATE OR REPLACE FUNCTION mi.actor_id() RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
 SET search_path=pg_catalog,mi AS $$
 SELECT coalesce((SELECT user_id FROM mi.service_principals WHERE database_role=session_user),
 nullif(current_setting('app.actor_id',true),'')::uuid) $$;
ALTER FUNCTION mi.actor_id() OWNER TO mi_guard;
CREATE FUNCTION mi.is_bound_service() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
 SET search_path=pg_catalog,mi AS $$
 SELECT EXISTS(SELECT 1 FROM mi.service_principals WHERE database_role=session_user) $$;
ALTER FUNCTION mi.is_bound_service() OWNER TO mi_guard;
REVOKE ALL ON FUNCTION mi.is_bound_service() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mi.actor_id(),mi.is_bound_service() TO mi_app;
-- Only the API role may handle passwords, invitations or interactive sessions.
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA identity FROM mi_app;
GRANT EXECUTE ON FUNCTION identity.login_lookup(text),identity.refresh_lookup(text),identity.invite_lookup(text),
 identity.accept_invite(text),identity.check_attempt(text),identity.record_attempt(text,boolean) TO mi_api;
REVOKE ALL ON identity.sessions,identity.invites,identity.auth_events FROM mi_app;
GRANT SELECT,INSERT,UPDATE ON identity.sessions TO mi_api;
GRANT SELECT,INSERT ON identity.invites TO mi_api;
GRANT INSERT ON identity.auth_events TO mi_api;
REVOKE INSERT ON identity.users,mi.organizations,mi.organization_memberships,mi.brand_memberships FROM mi_app;
GRANT INSERT ON identity.users,mi.organizations,mi.organization_memberships,mi.brand_memberships TO mi_api;
COMMIT;
