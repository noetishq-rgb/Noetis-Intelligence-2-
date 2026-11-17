BEGIN;
-- Keep the crawler's canonical field names when client setup replaces the intake.
CREATE OR REPLACE FUNCTION mi.submit_client_setup(bid uuid,setup jsonb) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mi,identity AS $$
 DECLARE site text;
 BEGIN
 IF mi.is_bound_service() OR NOT mi.brain_write(bid) OR NOT EXISTS(
 SELECT 1 FROM mi.client_accounts WHERE brand_id=bid) THEN RAISE EXCEPTION 'Client workspace unavailable' USING ERRCODE='42501'; END IF;
 IF jsonb_typeof(setup)<>'object' OR length(trim(setup->>'brand_name')) NOT BETWEEN 1 AND 300
 OR coalesce(jsonb_array_length(setup->'online_presence'),0)=0 OR coalesce(jsonb_array_length(setup->'objectives'),0)=0
 OR coalesce(length(trim(setup->>'category')),0)=0 THEN RAISE EXCEPTION 'Complete setup required' USING ERRCODE='22023'; END IF;
 SELECT value->>'url' INTO site FROM jsonb_array_elements(setup->'online_presence') WHERE value->>'platform'='website' LIMIT 1;
 UPDATE mi.brand_workspaces SET name=setup->>'brand_name',website_url=site,
 intake=setup||jsonb_build_object('name',setup->>'brand_name','website_url',site,'client_onboarding',true,
 'awaiting_configuration',NOT EXISTS(SELECT 1 FROM mi.jobs WHERE brand_id=bid AND kind='website_crawl'))
 WHERE id=bid;
 END $$;
COMMIT;
