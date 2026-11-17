BEGIN;
-- Let a brand-scoped research worker report its own recorded token usage.
CREATE POLICY research_usage_read ON mi.prompt_runs FOR SELECT TO mi_app
 USING(mi.has_brand_scope(brand_id,'research:run') AND EXISTS(
  SELECT 1 FROM mi.model_dispatches d WHERE d.id=prompt_runs.id AND d.brand_id=prompt_runs.brand_id
 ));
COMMIT;
