BEGIN;
ALTER TABLE mi.tool_calls ADD COLUMN attempt integer NOT NULL DEFAULT 1 CHECK(attempt BETWEEN 1 AND 3);
ALTER TABLE mi.tool_calls ADD COLUMN lease_id uuid;
COMMIT;
