BEGIN;
-- Session creator and actual submitting client can be different people.
ALTER TABLE mi.brand_onboarding_sessions ADD COLUMN onboarding_response_id uuid;
ALTER TABLE mi.brand_onboarding_sessions ADD CONSTRAINT session_research_submission
 FOREIGN KEY(onboarding_response_id,brand_id) REFERENCES mi.onboarding_responses(id,brand_id);
COMMIT;
