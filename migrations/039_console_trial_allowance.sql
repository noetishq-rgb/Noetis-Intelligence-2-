BEGIN;
-- At the pinned model rates, 75 requests of 8k input and 512 output tokens
-- reserve $2.376 total, leaving headroom below the shared $2.50 pilot ceiling.
ALTER SEQUENCE mi.console_trial_slots MAXVALUE 75;
COMMIT;
