BEGIN;
GRANT CREATE ON SCHEMA identity TO mi_guard;
-- Dedicated client identities are issued by an agency owner, never by public signup.
ALTER TABLE identity.users ADD COLUMN requires_password_change boolean NOT NULL DEFAULT false;
ALTER TABLE identity.users ADD COLUMN managed_by_organization_id uuid REFERENCES mi.organizations(id);
GRANT SELECT(requires_password_change,managed_by_organization_id) ON identity.users TO mi_api;

CREATE TABLE mi.client_accounts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), organization_id uuid NOT NULL,
 brand_id uuid NOT NULL, user_id uuid NOT NULL UNIQUE REFERENCES identity.users(id),
 created_by uuid NOT NULL REFERENCES identity.users(id), created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(brand_id,organization_id) REFERENCES mi.brand_workspaces(id,organization_id),
 FOREIGN KEY(organization_id,user_id) REFERENCES mi.organization_memberships(organization_id,user_id)
);
CREATE INDEX client_accounts_org ON mi.client_accounts(organization_id,created_at);
CREATE INDEX client_accounts_brand ON mi.client_accounts(brand_id);
ALTER TABLE mi.client_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE mi.client_accounts FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_read ON mi.client_accounts FOR SELECT TO mi_api USING(mi.can_org_admin(organization_id));
CREATE POLICY owner_insert ON mi.client_accounts FOR INSERT TO mi_api WITH CHECK(mi.can_org_admin(organization_id) AND created_by=mi.actor_id());
CREATE POLICY guard_read ON mi.client_accounts FOR SELECT TO mi_guard USING(true);
GRANT SELECT,INSERT ON mi.client_accounts TO mi_api;
GRANT SELECT ON mi.client_accounts TO mi_guard;
CREATE TRIGGER client_account_audit AFTER INSERT ON mi.client_accounts FOR EACH ROW EXECUTE FUNCTION mi.record_change();

-- Fixed, checked functions grant the API no direct password UPDATE authority.
GRANT INSERT ON identity.users TO mi_guard;
GRANT UPDATE(password_hash,requires_password_change,disabled) ON identity.users TO mi_guard;
CREATE POLICY managed_user_insert ON identity.users FOR INSERT TO mi_guard WITH CHECK(NOT is_service AND managed_by_organization_id IS NOT NULL);
CREATE POLICY managed_user_update ON identity.users FOR UPDATE TO mi_guard USING(NOT is_service) WITH CHECK(NOT is_service);
GRANT SELECT,UPDATE ON identity.sessions TO mi_guard;
CREATE POLICY guard_session_read ON identity.sessions FOR SELECT TO mi_guard USING(true);
CREATE POLICY guard_session_revoke ON identity.sessions FOR UPDATE TO mi_guard USING(true) WITH CHECK(true);

CREATE FUNCTION identity.create_client_user(oid uuid,uid uuid,address text,display_name text,encoded_password text) RETURNS uuid
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 BEGIN
 IF NOT mi.can_org_admin(oid) OR mi.is_bound_service() THEN RAISE EXCEPTION 'Owner access required' USING ERRCODE='42501'; END IF;
 IF encoded_password NOT LIKE '$argon2id$%' THEN RAISE EXCEPTION 'Password hash required' USING ERRCODE='22023'; END IF;
 INSERT INTO identity.users(id,email,full_name,password_hash,requires_password_change,managed_by_organization_id)
 VALUES(uid,lower(address),display_name,encoded_password,true,oid);
 RETURN uid;
 END $$;
ALTER FUNCTION identity.create_client_user(uuid,uuid,text,text,text) OWNER TO mi_guard;

CREATE FUNCTION identity.manage_client_user(oid uuid,uid uuid,operation text,encoded_password text DEFAULT NULL) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 BEGIN
 IF NOT mi.can_org_admin(oid) OR mi.is_bound_service() OR uid=mi.actor_id() OR NOT EXISTS(
 SELECT 1 FROM mi.client_accounts a JOIN identity.users u ON u.id=a.user_id
 WHERE a.organization_id=oid AND a.user_id=uid AND u.managed_by_organization_id=oid AND NOT u.is_service)
 THEN RAISE EXCEPTION 'Client account unavailable' USING ERRCODE='42501'; END IF;
 PERFORM 1 FROM identity.users WHERE id=uid FOR UPDATE;
 IF operation='reset_password' AND encoded_password LIKE '$argon2id$%' THEN
 UPDATE identity.users SET password_hash=encoded_password,requires_password_change=true WHERE id=uid;
 ELSIF operation='suspend' THEN UPDATE identity.users SET disabled=true WHERE id=uid;
 ELSIF operation='resume' THEN UPDATE identity.users SET disabled=false WHERE id=uid;
 ELSE RAISE EXCEPTION 'Invalid account operation' USING ERRCODE='22023'; END IF;
 UPDATE identity.sessions SET revoked_at=now() WHERE user_id=uid AND revoked_at IS NULL;
 END $$;
ALTER FUNCTION identity.manage_client_user(uuid,uuid,text,text) OWNER TO mi_guard;

CREATE FUNCTION identity.change_own_password(encoded_password text) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 BEGIN
 IF mi.is_bound_service() OR encoded_password NOT LIKE '$argon2id$%' OR NOT EXISTS(
 SELECT 1 FROM identity.users WHERE id=mi.actor_id() AND NOT disabled AND NOT is_service)
 THEN RAISE EXCEPTION 'Account unavailable' USING ERRCODE='42501'; END IF;
 PERFORM 1 FROM identity.users WHERE id=mi.actor_id() FOR UPDATE;
 UPDATE identity.users SET password_hash=encoded_password,requires_password_change=false WHERE id=mi.actor_id();
 UPDATE identity.sessions SET revoked_at=now() WHERE user_id=mi.actor_id() AND revoked_at IS NULL;
 END $$;
ALTER FUNCTION identity.change_own_password(text) OWNER TO mi_guard;

CREATE FUNCTION mi.client_account_directory(oid uuid) RETURNS jsonb
 LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 DECLARE result jsonb;
 BEGIN
 IF NOT mi.can_org_admin(oid) OR mi.is_bound_service() THEN RAISE EXCEPTION 'Owner access required' USING ERRCODE='42501'; END IF;
 SELECT coalesce(jsonb_agg(jsonb_build_object('id',a.id,'brand_id',a.brand_id,'brand_name',b.name,
 'email',u.email,'full_name',u.full_name,'status',CASE WHEN u.disabled THEN 'suspended' ELSE 'active' END,
 'requires_password_change',u.requires_password_change,'onboarding_status',coalesce(s.status,'not_started'),
 'created_at',a.created_at) ORDER BY a.created_at DESC),'[]'::jsonb) INTO result
 FROM mi.client_accounts a JOIN identity.users u ON u.id=a.user_id JOIN mi.brand_workspaces b ON b.id=a.brand_id
 LEFT JOIN LATERAL(SELECT status FROM mi.brand_onboarding_sessions WHERE brand_id=a.brand_id ORDER BY created_at DESC LIMIT 1) s ON true
 WHERE a.organization_id=oid;
 RETURN result;
 END $$;
ALTER FUNCTION mi.client_account_directory(uuid) OWNER TO mi_guard;

-- A client may submit its own setup without receiving broad workspace UPDATE privileges.
GRANT UPDATE(name,website_url,intake) ON mi.brand_workspaces TO mi_guard;
CREATE POLICY guard_client_setup ON mi.brand_workspaces FOR UPDATE TO mi_guard USING(mi.brain_write(id)) WITH CHECK(mi.brain_write(id));
CREATE FUNCTION mi.submit_client_setup(bid uuid,setup jsonb) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 DECLARE site text;
 BEGIN
 IF mi.is_bound_service() OR NOT mi.brain_write(bid) OR NOT EXISTS(
 SELECT 1 FROM mi.client_accounts WHERE brand_id=bid) THEN RAISE EXCEPTION 'Client workspace unavailable' USING ERRCODE='42501'; END IF;
 IF jsonb_typeof(setup)<>'object' OR length(trim(setup->>'brand_name')) NOT BETWEEN 1 AND 300
 OR coalesce(jsonb_array_length(setup->'online_presence'),0)=0 OR coalesce(jsonb_array_length(setup->'objectives'),0)=0
 OR coalesce(length(trim(setup->>'category')),0)=0 THEN RAISE EXCEPTION 'Complete setup required' USING ERRCODE='22023'; END IF;
 SELECT value->>'url' INTO site FROM jsonb_array_elements(setup->'online_presence') WHERE value->>'platform'='website' LIMIT 1;
 UPDATE mi.brand_workspaces SET name=setup->>'brand_name',website_url=site,
 intake=setup||jsonb_build_object('client_onboarding',true,'awaiting_configuration',true)
 WHERE id=bid;
 END $$;
ALTER FUNCTION mi.submit_client_setup(uuid,jsonb) OWNER TO mi_guard;

REVOKE ALL ON FUNCTION identity.create_client_user(uuid,uuid,text,text,text),identity.manage_client_user(uuid,uuid,text,text),identity.change_own_password(text),mi.client_account_directory(uuid),mi.submit_client_setup(uuid,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity.create_client_user(uuid,uuid,text,text,text),identity.manage_client_user(uuid,uuid,text,text),identity.change_own_password(text),mi.client_account_directory(uuid),mi.submit_client_setup(uuid,jsonb) TO mi_api;
REVOKE CREATE ON SCHEMA identity FROM mi_guard;
COMMIT;
