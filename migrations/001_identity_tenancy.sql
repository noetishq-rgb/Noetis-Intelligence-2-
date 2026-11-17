BEGIN;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
DO $$ BEGIN
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='mi_app') THEN CREATE ROLE mi_app NOLOGIN NOBYPASSRLS; END IF;
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='mi_guard') THEN CREATE ROLE mi_guard NOLOGIN BYPASSRLS; END IF;
END $$;
CREATE SCHEMA identity;
CREATE SCHEMA mi;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA identity,mi FROM PUBLIC;
GRANT USAGE ON SCHEMA mi,identity TO mi_app,mi_guard;
CREATE FUNCTION mi.actor_id() RETURNS uuid LANGUAGE sql STABLE AS $$
 SELECT nullif(current_setting('app.actor_id',true),'')::uuid $$;
CREATE TABLE identity.users (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), email text NOT NULL UNIQUE CHECK(email=lower(email)),
 full_name text NOT NULL, password_hash text, disabled boolean NOT NULL DEFAULT false,
 is_service boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now(),
 CHECK ((is_service AND password_hash IS NULL) OR (NOT is_service AND password_hash IS NOT NULL))
);
CREATE TABLE identity.sessions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES identity.users(id),
 refresh_hash text NOT NULL UNIQUE, family_id uuid NOT NULL, expires_at timestamptz NOT NULL,
 consumed_at timestamptz, revoked_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_family ON identity.sessions(family_id);
CREATE TABLE identity.auth_attempts (
 bucket text PRIMARY KEY, failures integer NOT NULL DEFAULT 0 CHECK(failures>=0),
 window_start timestamptz NOT NULL DEFAULT now(), blocked_until timestamptz
);
CREATE TABLE mi.organizations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text NOT NULL,
 owner_id uuid NOT NULL REFERENCES identity.users(id), currency text NOT NULL DEFAULT 'INR' CHECK(currency='INR'),
 locale text NOT NULL DEFAULT 'en', timezone text NOT NULL DEFAULT 'Asia/Kolkata',
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE mi.organization_memberships (
 organization_id uuid NOT NULL REFERENCES mi.organizations(id), user_id uuid NOT NULL REFERENCES identity.users(id),
 role text NOT NULL CHECK(role IN ('founder','admin','team','client','service')),
 PRIMARY KEY(organization_id,user_id), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE mi.brand_workspaces (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), brand_id uuid GENERATED ALWAYS AS(id) STORED UNIQUE,
 organization_id uuid NOT NULL REFERENCES mi.organizations(id), name text NOT NULL,
 website_url text NOT NULL CHECK(website_url ~ '^https?://'), intake jsonb NOT NULL CHECK(jsonb_typeof(intake)='object'),
 status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived','deletion_requested')),
 created_by uuid NOT NULL REFERENCES identity.users(id), created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(id,organization_id)
);
CREATE TABLE mi.brand_memberships (
 brand_id uuid NOT NULL, organization_id uuid NOT NULL, user_id uuid NOT NULL,
 scopes text[] NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(brand_id,user_id), FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(organization_id,user_id) REFERENCES mi.organization_memberships(organization_id,user_id)
);
CREATE TABLE identity.invites (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL REFERENCES mi.organizations(id),
 brand_id uuid NOT NULL, email text NOT NULL CHECK(email=lower(email)),
 role text NOT NULL CHECK(role IN ('admin','team','client')),
 scopes text[] NOT NULL DEFAULT '{}', token_hash text NOT NULL UNIQUE,
 expires_at timestamptz NOT NULL, accepted_at timestamptz, invited_by uuid NOT NULL REFERENCES identity.users(id),
 created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id)
);
-- Narrow definer predicates avoid recursive membership policies. No dynamic SQL.
GRANT SELECT ON mi.organizations,mi.organization_memberships,mi.brand_workspaces,mi.brand_memberships,identity.users TO mi_guard;
CREATE FUNCTION mi.org_role(oid uuid) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 SELECT m.role FROM mi.organization_memberships m JOIN identity.users u ON u.id=m.user_id
 WHERE m.organization_id=oid AND m.user_id=mi.actor_id() AND NOT u.disabled $$;
ALTER FUNCTION mi.org_role(uuid) OWNER TO mi_guard;
CREATE FUNCTION mi.can_org_admin(oid uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT coalesce(mi.org_role(oid) IN ('founder','admin'),false) $$;
CREATE FUNCTION mi.has_brand_scope(bid uuid, requested text) RETURNS boolean
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 SELECT coalesce(EXISTS(SELECT 1 FROM mi.brand_workspaces b JOIN mi.organization_memberships m
 ON m.organization_id=b.organization_id JOIN identity.users u ON u.id=m.user_id
 LEFT JOIN mi.brand_memberships bm ON bm.brand_id=b.id AND bm.user_id=m.user_id
 WHERE b.id=bid AND m.user_id=mi.actor_id() AND NOT u.disabled AND (
 m.role IN ('founder','admin') OR (bm.user_id IS NOT NULL AND (
 (m.role='client' AND requested IN ('brand:read','onboarding:read','onboarding:write','published:read','approval:decide')) OR
 (m.role IN ('team','service') AND (requested=ANY(bm.scopes)))
 )))),false) $$;
ALTER FUNCTION mi.has_brand_scope(uuid,text) OWNER TO mi_guard;
CREATE FUNCTION mi.staff_brand(bid uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT mi.has_brand_scope(bid,'internal:read') $$;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA mi FROM PUBLIC;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA mi TO mi_app,mi_guard;
ALTER TABLE identity.users ENABLE ROW LEVEL SECURITY; ALTER TABLE identity.users FORCE ROW LEVEL SECURITY;
CREATE POLICY users_self ON identity.users TO mi_app USING(id=mi.actor_id()) WITH CHECK(id=mi.actor_id() AND NOT is_service);
GRANT SELECT(id,email,full_name,disabled,is_service,created_at),INSERT ON identity.users TO mi_app;
ALTER TABLE identity.sessions ENABLE ROW LEVEL SECURITY; ALTER TABLE identity.sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY sessions_self ON identity.sessions TO mi_app USING(user_id=mi.actor_id()) WITH CHECK(user_id=mi.actor_id());
GRANT SELECT,INSERT,UPDATE ON identity.sessions TO mi_app;
ALTER TABLE mi.organizations ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.organizations FORCE ROW LEVEL SECURITY;
CREATE POLICY org_read ON mi.organizations FOR SELECT TO mi_app USING(mi.org_role(id) IS NOT NULL OR owner_id=mi.actor_id());
CREATE POLICY org_insert ON mi.organizations FOR INSERT TO mi_app WITH CHECK(owner_id=mi.actor_id());
GRANT SELECT,INSERT ON mi.organizations TO mi_app;
ALTER TABLE mi.organization_memberships ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.organization_memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY org_member_read ON mi.organization_memberships FOR SELECT TO mi_app USING(user_id=mi.actor_id() OR mi.can_org_admin(organization_id));
CREATE POLICY org_member_insert ON mi.organization_memberships FOR INSERT TO mi_app WITH CHECK(mi.can_org_admin(organization_id) OR (user_id=mi.actor_id() AND role='founder' AND organization_id IN(SELECT id FROM mi.organizations WHERE owner_id=mi.actor_id())));
GRANT SELECT,INSERT ON mi.organization_memberships TO mi_app;
ALTER TABLE mi.brand_workspaces ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.brand_workspaces FORCE ROW LEVEL SECURITY;
CREATE POLICY brands_read ON mi.brand_workspaces FOR SELECT TO mi_app USING(mi.has_brand_scope(id,'brand:read') OR mi.staff_brand(id));
CREATE POLICY brands_insert ON mi.brand_workspaces FOR INSERT TO mi_app WITH CHECK(mi.can_org_admin(organization_id) AND created_by=mi.actor_id());
CREATE POLICY brands_update ON mi.brand_workspaces FOR UPDATE TO mi_app USING(mi.can_org_admin(organization_id)) WITH CHECK(mi.can_org_admin(organization_id));
GRANT SELECT,INSERT,UPDATE ON mi.brand_workspaces TO mi_app;
ALTER TABLE mi.brand_memberships ENABLE ROW LEVEL SECURITY; ALTER TABLE mi.brand_memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY brand_member_read ON mi.brand_memberships FOR SELECT TO mi_app USING(user_id=mi.actor_id() OR mi.can_org_admin(organization_id));
CREATE POLICY brand_member_insert ON mi.brand_memberships FOR INSERT TO mi_app WITH CHECK(mi.can_org_admin(organization_id));
GRANT SELECT,INSERT ON mi.brand_memberships TO mi_app;
ALTER TABLE identity.invites ENABLE ROW LEVEL SECURITY; ALTER TABLE identity.invites FORCE ROW LEVEL SECURITY;
CREATE POLICY invite_admin ON identity.invites TO mi_app USING(mi.can_org_admin(organization_id)) WITH CHECK(mi.can_org_admin(organization_id) AND invited_by=mi.actor_id());
GRANT SELECT,INSERT ON identity.invites TO mi_app;
COMMIT;
