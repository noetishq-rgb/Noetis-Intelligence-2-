"""Render a narrowly scoped pre-research data reset for administrator execution.

Preserves accounts, memberships, service bindings, prompt templates and audit hashes.
Refuses charged/generated research, approvals and active work. Never used by HTTP.
"""
import argparse
from uuid import UUID
from psycopg import sql


TABLES = [
    'brand_brain_confirmations','brand_brain_thread_events','brand_brain_comments',
    'brand_brain_threads','brand_brain_reviews','brand_brain_versions','brand_onboarding_sessions',
    'idempotency_keys','tool_calls','crawl_pages','onboarding_responses','workflow_events',
    'brand_profiles','jobs','research_sources','artifacts','workflow_instances',
]


def reset_sql(brand_id:UUID, actor_id:UUID, expected_name:str):
    tables=sql.SQL(',').join(sql.Literal(t) for t in TABLES)
    return sql.SQL('''DO $reset$
    DECLARE bid uuid:={brand}::uuid; actor uuid:={actor}::uuid; org uuid; trigger_row record; table_name text;
    BEGIN
      PERFORM set_config('app.actor_id',actor::text,true);
      PERFORM pg_advisory_xact_lock(hashtextextended('brain:'||bid::text,0));
      SELECT organization_id INTO org FROM mi.brand_workspaces
       WHERE id=bid AND lower(name)=lower({name}) FOR UPDATE;
      IF org IS NULL OR NOT mi.can_org_admin(org) THEN
       RAISE EXCEPTION 'Authorized owner and exact test workspace required'; END IF;
      PERFORM 1 FROM mi.workflow_instances WHERE brand_id=bid FOR UPDATE;
      PERFORM 1 FROM mi.jobs WHERE brand_id=bid FOR UPDATE;
      IF EXISTS(SELECT 1 FROM mi.jobs WHERE brand_id=bid AND (status IN('queued','running','retry') OR kind<>'website_crawl'))
       OR EXISTS(SELECT 1 FROM mi.model_dispatches WHERE brand_id=bid)
       OR EXISTS(SELECT 1 FROM mi.approvals WHERE brand_id=bid)
       OR EXISTS(SELECT 1 FROM mi.research_dossiers WHERE brand_id=bid)
       THEN RAISE EXCEPTION 'Reset is restricted to idle pre-research test data'; END IF;
      -- Transactional table locks prevent any session from writing while the
      -- listed append-only guards are suspended. RLS, FKs and audit triggers stay on.
      FOR table_name IN SELECT unnest(ARRAY[{tables}]) LOOP
       EXECUTE format('LOCK TABLE mi.%I IN ACCESS EXCLUSIVE MODE NOWAIT',table_name);
      END LOOP;
      FOR trigger_row IN SELECT c.relname,t.tgname FROM pg_trigger t
       JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
       JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace pn ON pn.oid=p.pronamespace
       WHERE n.nspname='mi' AND pn.nspname='mi' AND p.proname='reject_mutation'
       AND c.relname=ANY(ARRAY[{tables}]) AND t.tgenabled='O'
      LOOP EXECUTE format('ALTER TABLE mi.%I DISABLE TRIGGER %I',trigger_row.relname,trigger_row.tgname); END LOOP;
      FOR table_name IN SELECT unnest(ARRAY[{tables}]) LOOP
       EXECUTE format('DELETE FROM mi.%I WHERE brand_id=$1',table_name) USING bid;
      END LOOP;
      UPDATE mi.brand_workspaces SET website_url=NULL,
       intake=jsonb_build_object('client_onboarding',true,'awaiting_configuration',true)
       WHERE id=bid;
      INSERT INTO mi.workflow_instances(organization_id,brand_id,created_by) VALUES(org,bid,actor);
      FOR trigger_row IN SELECT c.relname,t.tgname FROM pg_trigger t
       JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
       JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace pn ON pn.oid=p.pronamespace
       WHERE n.nspname='mi' AND pn.nspname='mi' AND p.proname='reject_mutation'
       AND c.relname=ANY(ARRAY[{tables}]) AND t.tgenabled='D'
      LOOP EXECUTE format('ALTER TABLE mi.%I ENABLE TRIGGER %I',trigger_row.relname,trigger_row.tgname); END LOOP;
      INSERT INTO mi.audit_logs(organization_id,brand_id,actor_id,action,entity_type,entity_id,output_hash)
       VALUES(org,bid,actor,'RESET','pre_research_test_workspace',bid,
        encode(mi.digest('Owner requested fresh onboarding; account access preserved','sha256'),'hex'));
    END $reset$;''').format(brand=sql.Literal(str(brand_id)),actor=sql.Literal(str(actor_id)),name=sql.Literal(expected_name),tables=tables).as_string()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--brand-id',type=UUID,required=True)
    parser.add_argument('--actor-id',type=UUID,required=True)
    parser.add_argument('--expected-name',required=True)
    args=parser.parse_args()
    print(reset_sql(args.brand_id,args.actor_id,args.expected_name))
