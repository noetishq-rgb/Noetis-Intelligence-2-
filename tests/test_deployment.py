from pathlib import Path
import json
import pytest
from fastapi.testclient import TestClient
from app.deployment_migrations import managed_sql
from app.db import transaction
from app.config import settings


def test_vercel_manifest_installs_locked_runtime_dependencies():
    import tomllib
    root=Path(__file__).parents[1]
    manifest=tomllib.loads((root/'pyproject.toml').read_text())
    declared=manifest['project']['dependencies']
    locked=set((root/'requirements.lock').read_text().splitlines())
    assert set(declared)<=locked
    assert {'fastapi','psycopg','psycopg-binary','argon2-cffi','PyJWT','pydantic-settings'}<={line.split('==')[0] for line in declared}


def test_managed_compatibility_keeps_original_files_and_uses_read_policies(owner):
    rows=owner.execute("SELECT rolcanlogin,rolsuper,rolbypassrls FROM pg_roles WHERE rolname='mi_guard'").fetchone()
    assert not rows['rolcanlogin'] and not rows['rolsuper']
    forced=owner.execute("SELECT count(*) AS n FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='mi' AND c.relkind='r' AND c.relrowsecurity AND c.relforcerowsecurity").fetchone()['n']
    assert forced>=30
    assert owner.execute("SELECT count(*) AS n FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname IN('mi','identity') AND NOT EXISTS(SELECT 1 FROM unnest(p.proconfig) config WHERE config LIKE 'search_path=%')").fetchone()['n']==0
    if not rows['rolbypassrls']:
        assert owner.execute("SELECT count(*) AS n FROM pg_policies WHERE policyname='managed_guard_read'").fetchone()['n']>10
        assert owner.execute("SELECT count(*) AS n FROM pg_policies WHERE policyname='managed_guard_update'").fetchone()['n']==1
    initial=(Path(__file__).parents[1]/'migrations/001_identity_tenancy.sql').read_text()
    assert 'GRANT CREATE ON SCHEMA mi TO mi_guard;' not in initial
    assert 'GRANT CREATE ON SCHEMA mi TO mi_guard;' in managed_sql(initial,'001_identity_tenancy.sql')


def test_deployment_surface_serves_ui_and_keeps_authentication(api,monkeypatch):
    # Load after the core app is initialized; the entrypoint adds the static mount
    # and guard before constructing the fresh application client below.
    import subprocess,os,sys
    script="""
from fastapi.testclient import TestClient
from vercel_entry import app
with TestClient(app) as c:
 assert c.get('/').status_code==200
 assert 'data-environment="live-pilot"' in c.get('/').text
 assert c.get('/brain.js').status_code==200
 assert c.get('/v1/brands').status_code==401
 assert c.get('/.env.production.example').status_code==404
 assert c.get('/health').status_code==200
print('Deployment surface verified')
"""
    result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,env=os.environ)
    assert result.returncode==0,result.stderr[-1500:]
