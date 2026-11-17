BEGIN;
-- INSERT RETURNING evaluates SELECT policy before a stable function can observe
-- the new workspace in its own table. Authorize administrators directly by org.
DROP POLICY brands_read ON mi.brand_workspaces;
CREATE POLICY brands_read ON mi.brand_workspaces FOR SELECT TO mi_app
 USING(mi.can_org_admin(organization_id) OR mi.has_brand_scope(id,'brand:read') OR mi.staff_brand(id));
COMMIT;
