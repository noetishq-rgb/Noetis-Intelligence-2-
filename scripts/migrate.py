"""Administrative, checksum-locked migration runner. Never runs inside web requests."""
import argparse,hashlib,os,sys
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
load_dotenv()
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
from app.deployment_migrations import prepare_managed,managed_sql,before_managed,finalize_managed,effective_digest


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--profile',choices=['native','supabase'],default='native')
    args=parser.parse_args()
    ledger='mi_meta.schema_migrations' if args.profile=='supabase' else 'public.schema_migrations'
    with psycopg.connect(os.environ['MI_MIGRATION_DATABASE_URL'],row_factory=dict_row,autocommit=True) as c:
        c.execute('SELECT pg_advisory_lock(786421019)')
        if args.profile=='supabase':c.execute('CREATE SCHEMA IF NOT EXISTS mi_meta')
        try:
            c.execute(f'CREATE TABLE IF NOT EXISTS {ledger}(name text PRIMARY KEY,sha256 text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())')
            c.execute(f"ALTER TABLE {ledger} ADD COLUMN IF NOT EXISTS profile text NOT NULL DEFAULT 'native'")
            c.execute(f'ALTER TABLE {ledger} ADD COLUMN IF NOT EXISTS effective_sha256 text')
            c.execute(f'ALTER TABLE {ledger} ENABLE ROW LEVEL SECURITY')
            c.execute(f'ALTER TABLE {ledger} FORCE ROW LEVEL SECURITY')
            c.execute(f'REVOKE ALL ON {ledger} FROM PUBLIC')
            for exposed_role in ['anon','authenticated','service_role']:
                if c.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(exposed_role,)).fetchone():
                    from psycopg import sql
                    c.execute(sql.SQL('REVOKE ALL ON {}.schema_migrations FROM {}').format(sql.Identifier('mi_meta' if args.profile=='supabase' else 'public'),sql.Identifier(exposed_role)))
            if args.profile=='supabase':prepare_managed(c)
            for path in sorted((root/'migrations').glob('*.sql')):
                raw=path.read_text();sha=hashlib.sha256(path.read_bytes()).hexdigest()
                effective=managed_sql(raw,path.name) if args.profile=='supabase' else raw
                digest=effective_digest(effective,managed=args.profile=='supabase')
                old=c.execute(f'SELECT * FROM {ledger} WHERE name=%s',(path.name,)).fetchone()
                if old:
                    if old['sha256']!=sha or old['profile']!=args.profile or old['effective_sha256'] not in {None,digest}:
                        raise RuntimeError('Applied migration source, profile or effective SQL changed: '+path.name)
                    continue
                with c.transaction():
                    if args.profile=='supabase':before_managed(c)
                    c.execute(effective.strip().removeprefix('BEGIN;').removesuffix('COMMIT;'))
                    if args.profile=='supabase':finalize_managed(c)
                    c.execute(f'INSERT INTO {ledger}(name,sha256,profile,effective_sha256) VALUES(%s,%s,%s,%s)',(path.name,sha,args.profile,digest))
                print('Applied',path.name)
        finally:c.execute('SELECT pg_advisory_unlock(786421019)')

if __name__=='__main__':main()
