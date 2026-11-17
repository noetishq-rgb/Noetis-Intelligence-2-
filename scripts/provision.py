"""Administrative bootstrap only. Passwords come from environment or hidden stdin."""
import argparse
import getpass
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4
import psycopg
from dotenv import load_dotenv
load_dotenv()
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    runtime = sub.add_parser("runtime-role")
    runtime.add_argument("--name", default="mi_runtime")
    runtime.add_argument("--kind", choices=["api", "worker"], default="api")
    founder = sub.add_parser("founder")
    founder.add_argument("--email", required=True)
    founder.add_argument("--name", required=True)
    founder.add_argument("--organization", required=True)
    service = sub.add_parser("crawl-service")
    service.add_argument("--organization-id", required=True, type=UUID)
    service.add_argument("--brand-id", required=True, type=UUID)
    service.add_argument("--database-role", required=True)
    research = sub.add_parser("research-service")
    research.add_argument("--organization-id", required=True, type=UUID)
    research.add_argument("--brand-id", required=True, type=UUID)
    research.add_argument("--database-role", required=True)
    planning = sub.add_parser("planning-service")
    planning.add_argument("--organization-id", required=True, type=UUID)
    planning.add_argument("--brand-id", required=True, type=UUID)
    planning.add_argument("--database-role", required=True)
    args = parser.parse_args()
    with psycopg.connect(os.environ["MI_MIGRATION_DATABASE_URL"]) as connection:
        if args.command == "runtime-role":
            password = os.environ.get("MI_RUNTIME_PASSWORD") or getpass.getpass("Runtime database password: ")
            if len(password) < 16:
                raise SystemExit("Use at least 16 characters")
            parent = "mi_api" if args.kind == "api" else "mi_app"
            connection.execute(sql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS IN ROLE {} PASSWORD {}").format(sql.Identifier(args.name), sql.Identifier(parent), sql.Literal(password)))
            print("Created runtime login:", args.name)
        elif args.command == "founder":
            from app.auth import password_hash
            password = getpass.getpass("Founder password: ")
            if not 12 <= len(password) <= 128:
                raise SystemExit("Use 12 to 128 characters")
            actor, org = uuid4(), uuid4()
            connection.execute("SELECT set_config('app.actor_id',%s,true)", (str(actor),))
            connection.execute("INSERT INTO identity.users(id,email,full_name,password_hash) VALUES(%s,%s,%s,%s)", (actor, args.email.lower(), args.name, password_hash(password)))
            connection.execute("INSERT INTO mi.organizations(id,name,owner_id) VALUES(%s,%s,%s)", (org, args.organization, actor))
            connection.execute("INSERT INTO mi.organization_memberships(organization_id,user_id,role) VALUES(%s,%s,'founder')", (org, actor))
            print("Founder:", actor, "Organization:", org)
        else:
            role = connection.execute("SELECT rolname,rolsuper,rolbypassrls,pg_has_role(oid,'mi_api','MEMBER') AS api_member FROM pg_roles WHERE rolname=%s", (args.database_role,)).fetchone()
            if role is None or role[1] or role[2] or role[3]:
                raise SystemExit("Choose a dedicated non-API worker database login")
            actor = uuid4()
            connection.execute("SELECT set_config('app.actor_id',%s,true)", (str(actor),))
            name = "Planning worker" if args.command == "planning-service" else "Research worker" if args.command == "research-service" else "Website crawl worker"
            connection.execute("INSERT INTO identity.users(id,email,full_name,is_service) VALUES(%s,%s,%s,true)", (actor, f"service-{actor}@internal.invalid", name))
            connection.execute("INSERT INTO mi.organization_memberships(organization_id,user_id,role) VALUES(%s,%s,'service')", (args.organization_id, actor))
            worker_scopes = ["brand:read", "profile:read", "job:read", "workflow:read", "artifact:write"]
            worker_scopes += ["planning:run"] if args.command == "planning-service" else ["research:run"] if args.command == "research-service" else ["crawl:run", "profile:write"]
            connection.execute("INSERT INTO mi.brand_memberships(brand_id,organization_id,user_id,scopes) VALUES(%s,%s,%s,%s)",
                (args.brand_id, args.organization_id, actor, worker_scopes))
            connection.execute("INSERT INTO mi.service_principals(database_role,user_id) VALUES(%s,%s)", (args.database_role, actor))
            print("Service user:", actor)


if __name__ == "__main__":
    main()
