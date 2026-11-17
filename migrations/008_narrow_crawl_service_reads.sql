BEGIN;
-- A crawler need not read prompts, costs, research or other internal deliverables.
ALTER POLICY internal_read ON mi.brand_profiles USING(mi.staff_brand(brand_id) OR mi.has_brand_scope(brand_id,'profile:read'));
ALTER POLICY scoped_read ON mi.jobs USING(mi.staff_brand(brand_id) OR mi.has_brand_scope(brand_id,'job:read'));
ALTER POLICY scoped_read ON mi.workflow_instances USING(mi.staff_brand(brand_id) OR mi.has_brand_scope(brand_id,'workflow:read'));
ALTER TABLE mi.brand_profiles ADD CHECK(
 profile ? 'brand_id' AND profile->>'brand_id'=brand_id::text
 AND profile->>'version'=version::text AND profile->>'approval_status'=status
 AND profile->>'schema_version'='1.0');
COMMIT;
