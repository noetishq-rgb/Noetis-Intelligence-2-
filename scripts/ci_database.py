"""Only disposable CI/local test databases. Never run against a hosted project."""
import os
import psycopg
from psycopg.conninfo import conninfo_to_dict
args=conninfo_to_dict(os.environ['MI_TEST_BOOTSTRAP_URL'])
if args.get('host') not in {'127.0.0.1','localhost'}:raise SystemExit('Disposable database bootstrap only allows loopback hosts')
with psycopg.connect(os.environ['MI_TEST_BOOTSTRAP_URL'],autocommit=True) as c:
    c.execute("CREATE ROLE managed_owner LOGIN CREATEROLE BYPASSRLS PASSWORD 'ci-managed-only-not-production'")
    c.execute('CREATE DATABASE noetis_ci OWNER managed_owner')
args['dbname']='noetis_ci'
with psycopg.connect(**args,autocommit=True) as c:
    c.execute('CREATE SCHEMA extensions AUTHORIZATION managed_owner')
    c.execute('CREATE EXTENSION pgcrypto WITH SCHEMA extensions')
print('Disposable managed-role CI database prepared.')
