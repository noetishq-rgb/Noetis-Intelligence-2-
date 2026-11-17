import os
from uuid import uuid4
import pytest
import psycopg
from psycopg.rows import dict_row
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import SecretStr
from fastapi.testclient import TestClient

os.environ.setdefault("MI_JWT_SECRET", "test-only-not-for-production-012345678901234567890")
os.environ.setdefault("MI_ALLOW_SIGNUP", "true")


@pytest.fixture
def owner():
    if not os.environ.get("MI_TEST_OWNER_URL"):
        pytest.skip("Set MI_TEST_OWNER_URL and MI_DATABASE_URL for PostgreSQL integration tests")
    with psycopg.connect(os.environ["MI_TEST_OWNER_URL"], row_factory=dict_row) as connection:
        yield connection


@pytest.fixture
def api(owner):
    from app.main import app
    with TestClient(app) as client:
        yield client


@pytest.fixture
def agency(api):
    email = f"founder-{uuid4()}@example.com"
    password = "Phase1-test-password!"
    response = api.post("/v1/auth/register", json={"email": email, "password": password, "full_name": "Test Founder", "organization_name": "Test Agency"})
    assert response.status_code == 201, response.text
    tokens = response.json()
    headers = {"Authorization": "Bearer " + tokens["access_token"]}
    organizations = api.get("/v1/organizations", headers=headers).json()
    assert len(organizations) == 1
    return {"email": email, "password": password, "tokens": tokens, "headers": headers, "organization": organizations[0]["id"]}


@pytest.fixture
def brand_factory(api):
    def create(agency, name="Sample Brand"):
        response = api.post(f"/v1/organizations/{agency['organization']}/brands",
            headers={**agency["headers"], "Idempotency-Key": str(uuid4())},
            json={"name": name, "website_url": "https://example.com", "target_geographies": ["India"], "languages": ["en"]})
        assert response.status_code == 201, response.text
        return response.json()
    return create


@pytest.fixture
def service_factory(owner):
    def create(brand, kind="crawl"):
        actor = uuid4()
        owner.execute("SELECT set_config('app.actor_id',%s,true)", (str(actor),))
        owner.execute("INSERT INTO identity.users(id,email,full_name,is_service) VALUES(%s,%s,'Test crawl service',true)", (actor, f"worker-{actor}@example.com"))
        owner.execute("INSERT INTO mi.organization_memberships(organization_id,user_id,role) VALUES(%s,%s,'service')", (brand["brand"]["organization_id"], actor))
        owner.execute("INSERT INTO mi.brand_memberships(organization_id,brand_id,user_id,scopes) VALUES(%s,%s,%s,%s)",
            (brand["brand"]["organization_id"], brand["brand"]["id"], actor, ["brand:read", "profile:read", "job:read", "workflow:read", "artifact:write"] + (["planning:run"] if kind == "planning" else ["research:run"] if kind == "research" else ["crawl:run", "profile:write"])))
        role = "mi_test_worker_" + actor.hex
        owner.execute(sql.SQL("CREATE ROLE {} LOGIN IN ROLE mi_app PASSWORD {}").format(sql.Identifier(role), sql.Literal("phase1-local-worker-only")))
        owner.execute("INSERT INTO mi.service_principals(database_role,user_id) VALUES(%s,%s)", (role, actor))
        owner.commit()
        params = conninfo_to_dict(os.environ["MI_TEST_OWNER_URL"])
        params.update(user=role, password="phase1-local-worker-only")
        from app.config import settings
        settings().worker_database_url = SecretStr(make_conninfo(**params))
        return actor
    return create
