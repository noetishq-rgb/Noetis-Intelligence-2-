BEGIN;
-- A research worker may reuse public captures only inside its assigned brand.
-- This grants no crawl insertion, job execution or cross-brand access.
CREATE POLICY research_capture_read ON mi.crawl_pages FOR SELECT TO mi_app
 USING(mi.has_brand_scope(brand_id,'research:run'));
COMMIT;
