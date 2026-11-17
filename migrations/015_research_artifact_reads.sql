BEGIN;
ALTER POLICY internal_read ON mi.artifacts USING(mi.staff_brand(brand_id) OR
 (kind IN('brand_profile','research_dossier') AND mi.has_brand_scope(brand_id,'research:run')));
COMMIT;
