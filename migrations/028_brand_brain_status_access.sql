BEGIN;
-- Only the sanitized, brand-authorized status function is callable; the API role gains no new job grants.
GRANT SELECT ON mi.jobs TO mi_guard;
COMMIT;
