BEGIN;
-- Existing decisions remain historical. New approvals need explicit human semantic review.
-- NOT VALID avoids rewriting/revoking old append-only review records; new inserts are checked.
ALTER TABLE mi.research_reviews ADD CONSTRAINT research_semantic_review_required CHECK (
 decision <> 'approved' OR (
   coalesce(resolutions->'semantic_review'->'source_quality_checked' = 'true'::jsonb,false)
   AND coalesce(resolutions->'semantic_review'->'geography_checked' = 'true'::jsonb,false)
   AND coalesce(resolutions->'semantic_review'->'contradictions_checked' = 'true'::jsonb,false)
   AND coalesce(resolutions->'semantic_review'->'coverage_checked' = 'true'::jsonb,false)
   AND CASE WHEN jsonb_typeof(resolutions->'semantic_review'->'claim_assessments')='array'
       THEN jsonb_array_length(resolutions->'semantic_review'->'claim_assessments')>0 ELSE false END
   AND coalesce(resolutions->'semantic_review'->'unresolved_issues' = '[]'::jsonb,false)
   AND coalesce(resolutions->'validation_report'->>'validator_version' = '3.0',false)
   AND coalesce(resolutions->'validation_report'->>'schema_status' = 'passed',false)
   AND coalesce(resolutions->'validation_report'->>'provenance_status' = 'passed',false)
 )
) NOT VALID;
COMMIT;
