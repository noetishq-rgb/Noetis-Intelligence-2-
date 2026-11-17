"""Additive Supabase compatibility for immutable original migration files.

Managed Postgres has no superuser CREATE BYPASSRLS capability. A NOLOGIN guard
uses explicit SELECT policies instead. Application logins never inherit that role.
"""
import hashlib
from pathlib import Path
from psycopg import sql


def prepare_managed(c):
    owner=c.execute('SELECT current_user AS name,rolsuper,rolbypassrls,rolcreaterole FROM pg_roles WHERE rolname=current_user').fetchone()
    if not owner['rolsuper'] and not (owner['rolbypassrls'] and owner['rolcreaterole']):
        raise RuntimeError('Supabase migrations require its administrative postgres connection with CREATEROLE and BYPASSRLS; never use the runtime login')
    for name,parent in [('mi_app',None),('mi_guard',None),('mi_api','mi_app')]:
        row=c.execute('SELECT rolcanlogin,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=%s',(name,)).fetchone()
        if row:
            if row['rolcanlogin'] or row['rolsuper'] or row['rolbypassrls']:
                raise RuntimeError('Managed migration role name collides with an incompatible existing role')
        else:
            suffix=sql.SQL(' IN ROLE {}').format(sql.Identifier(parent)) if parent else sql.SQL('')
            c.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOBYPASSRLS{}').format(sql.Identifier(name),suffix))
        # Needed to transfer ownership on PostgreSQL 16+. Not granted to any runtime login.
        c.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier(name),sql.Identifier(owner['name'])))
    c.commit()


def managed_sql(raw,name):
    raw=raw.replace('public.digest','mi.digest')
    if name.startswith('001_'):
        marker='GRANT USAGE ON SCHEMA mi,identity TO mi_app,mi_guard;'
        if marker not in raw:raise RuntimeError('Unexpected initial migration; managed ownership setup needs review')
        raw=raw.replace(marker,marker+'\nGRANT CREATE ON SCHEMA mi TO mi_guard;',1)
        raw=raw.replace('REVOKE CREATE ON SCHEMA public FROM PUBLIC;','-- Existing public schema privileges are preserved by the managed profile.')
    return raw


def before_managed(c):
    if c.execute("SELECT to_regnamespace('mi') AS value").fetchone()['value']:
        c.execute('GRANT CREATE ON SCHEMA mi TO mi_guard')


def finalize_managed(c):
    # Supabase installs pgcrypto in extensions, while the original migrations
    # explicitly call public.digest. Add thin invoker wrappers only when absent.
    ext=c.execute("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='pgcrypto'").fetchone()
    if ext:
        namespace=ext['nspname']
        c.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO mi_app,mi_guard').format(sql.Identifier(namespace)))
        for arg in ['bytea','text']:
            if not c.execute('SELECT to_regprocedure(%s) AS value',(f'mi.digest({arg},text)',)).fetchone()['value']:
                body=sql.SQL('SELECT {}.digest($1,$2)').format(sql.Identifier(namespace))
                c.execute(sql.SQL('CREATE FUNCTION mi.digest({},text) RETURNS bytea LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE SET search_path=pg_catalog AS {}').format(sql.SQL(arg),sql.Literal(body.as_string(c))))
    c.execute('REVOKE EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) FROM PUBLIC')
    c.execute('GRANT EXECUTE ON FUNCTION mi.digest(bytea,text),mi.digest(text,text) TO mi_app,mi_guard')
    tables=c.execute("""SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname IN('mi','identity') AND c.relkind='r' AND c.relrowsecurity
        AND has_table_privilege('mi_guard',c.oid,'SELECT')""").fetchall()
    for row in tables:
        if not c.execute("SELECT 1 FROM pg_policies WHERE schemaname=%s AND tablename=%s AND policyname='managed_guard_read'",(row['nspname'],row['relname'])).fetchone():
            c.execute(sql.SQL('CREATE POLICY managed_guard_read ON {}.{} FOR SELECT TO mi_guard USING(true)').format(sql.Identifier(row['nspname']),sql.Identifier(row['relname'])))
    # submit_intake is the sole guard-owned writer in the original history.
    # Preserve its existing granted UPDATE authority without bypassing RLS.
    if c.execute("SELECT to_regclass('mi.workflow_instances') AS value").fetchone()['value']:
        if c.execute("SELECT has_table_privilege('mi_guard','mi.workflow_instances','UPDATE') AS ok").fetchone()['ok']:
            if not c.execute("SELECT 1 FROM pg_policies WHERE schemaname='mi' AND tablename='workflow_instances' AND policyname='managed_guard_update'").fetchone():
                c.execute('CREATE POLICY managed_guard_update ON mi.workflow_instances FOR UPDATE TO mi_guard USING(true) WITH CHECK(true)')
    if c.execute("SELECT to_regnamespace('mi') AS value").fetchone()['value']:
        c.execute('REVOKE CREATE ON SCHEMA mi FROM mi_guard')


def effective_digest(raw,managed=False):
    support=Path(__file__).read_bytes() if managed else b''
    return hashlib.sha256(raw.encode()+support).hexdigest()
