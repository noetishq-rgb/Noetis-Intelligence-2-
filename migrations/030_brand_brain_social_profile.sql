BEGIN;
ALTER TABLE mi.brand_profiles DROP CONSTRAINT brand_profiles_check;
ALTER TABLE mi.brand_profiles ADD CHECK(
 profile ? 'brand_id' AND profile->>'brand_id'=brand_id::text AND profile->>'version'=version::text
 AND profile->>'approval_status'=status AND profile->>'schema_version' IN('1.0','1.1'));
COMMIT;
