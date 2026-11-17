from contextlib import contextmanager
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.config import settings


def set_actor(connection, actor_id: UUID | str):
    # Transaction-local context cannot leak to a later request or pooled connection.
    connection.execute("SELECT set_config('app.actor_id', %s, true)", (str(actor_id),))


@contextmanager
def transaction(actor_id: UUID | str | None = None, database_url: str | None = None):
    with psycopg.connect(database_url or settings().database_url.get_secret_value(), row_factory=dict_row) as connection:
        connection.execute("SET LOCAL statement_timeout = '10s'")
        connection.execute("SET LOCAL lock_timeout = '3s'")
        connection.execute("SET LOCAL timezone = 'UTC'")
        if actor_id:
            set_actor(connection, actor_id)
        yield connection


def check_runtime_role(connection):
    row = connection.execute("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
    if row["rolsuper"] or row["rolbypassrls"]:
        raise RuntimeError("Runtime database role must not bypass RLS")
    dangerous = connection.execute("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE pg_has_role(current_user,oid,'MEMBER') AND (rolsuper OR rolbypassrls OR rolname='mi_guard')) AS allowed").fetchone()
    if dangerous["allowed"]:
        raise RuntimeError("Runtime database role has a privileged membership")
