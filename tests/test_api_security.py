from uuid import UUID, uuid4
import pytest
import psycopg
from psycopg.types.json import Jsonb
from app.db import transaction
from app.services.crawl import CrawledPage
from app.worker import run_once


def invite_user(api, agency, brand, role, scopes=None):
    invited = api.post(f"/v1/organizations/{agency['organization']}/invitations", headers=agency["headers"], json={
        "brand_id": brand["brand"]["id"], "email": f"member-{uuid4()}@example.com", "role": role, "scopes": scopes or []})
    assert invited.status_code == 201, invited.text
    accepted = api.post("/v1/auth/accept-invitation", json={"invitation_token": invited.json()["invitation_token"], "full_name": "Invited User", "password": "Phase1-client-password!"})
    assert accepted.status_code == 201, accepted.text
    return {"Authorization": "Bearer " + accepted.json()["access_token"]}


def fake_crawl(url, max_pages, trace=None):
    if trace:
        trace({"endpoint": url, "response_status": 200, "error_code": None, "latency_ms": 1})
    return [CrawledPage(url, 200, "Sample title", "A supplied website description.", "Public page text", "a" * 64)]


def test_cross_organization_and_same_org_client_isolation(api, agency, brand_factory):
    first = brand_factory(agency, "First")
    second = brand_factory(agency, "Second")
    client_headers = invite_user(api, agency, first, "client")
    assert {b["id"] for b in api.get("/v1/brands", headers=client_headers).json()} == {first["brand"]["id"]}
    for route in ["", "/profile", "/onboarding", "/onboarding/questionnaire", "/audit"]:
        assert api.get(f"/v1/brands/{second['brand']['id']}{route}", headers=client_headers).status_code == 404
    assert api.get(f"/v1/brands/{first['brand']['id']}/profile", headers=client_headers).status_code == 404
    assert api.get(f"/v1/brands/{first['brand']['id']}/onboarding/questionnaire", headers=client_headers).status_code == 200
    assert api.post(f"/v1/brands/{first['brand']['id']}/crawls", headers={**client_headers, "Idempotency-Key": str(uuid4())}, json={}).status_code == 404
    foreign = api.post("/v1/auth/register", json={"email": f"other-{uuid4()}@example.com", "password": "Phase1-other-password!", "full_name": "Other", "organization_name": "Other agency"}).json()
    assert api.get(f"/v1/brands/{first['brand']['id']}", headers={"Authorization": "Bearer " + foreign["access_token"]}).status_code == 404


def test_team_has_only_assigned_brand_and_workflows(api, agency, brand_factory):
    first, second = brand_factory(agency), brand_factory(agency)
    headers = invite_user(api, agency, first, "team", ["brand:read", "internal:read"])
    assert api.get(f"/v1/brands/{first['brand']['id']}/profile", headers=headers).status_code == 200
    assert api.get(f"/v1/brands/{second['brand']['id']}", headers=headers).status_code == 404
    assert api.post(f"/v1/brands/{first['brand']['id']}/crawls", headers={**headers, "Idempotency-Key": str(uuid4())}, json={}).status_code == 404
    assert api.get(f"/v1/brands/{first['brand']['id']}/audit", headers=headers).status_code == 404


def test_registration_does_not_accept_role_and_errors_do_not_echo_password(api):
    secret = "Do-not-echo-this-secret!"
    result = api.post("/v1/auth/register", json={"email": "broken", "password": secret, "full_name": "X", "organization_name": "Y", "role": "admin"})
    assert result.status_code == 422
    assert secret not in result.text


def test_brand_creation_is_idempotent_and_detects_payload_changes(api, agency):
    path = f"/v1/organizations/{agency['organization']}/brands"
    headers = {**agency["headers"], "Idempotency-Key": str(uuid4())}
    body = {"name": "One brand", "website_url": "https://example.com"}
    first = api.post(path, headers=headers, json=body)
    second = api.post(path, headers=headers, json=body)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert api.post(path, headers=headers, json={**body, "name": "Changed"}).status_code == 409


def test_refresh_rotation_replay_revokes_successor(api, agency):
    refreshed = api.post("/v1/auth/refresh", json={"refresh_token": agency["tokens"]["refresh_token"]})
    assert refreshed.status_code == 200, refreshed.text
    assert api.get("/v1/me", headers=agency["headers"]).status_code == 401
    successor = {"Authorization": "Bearer " + refreshed.json()["access_token"]}
    assert api.get("/v1/me", headers=successor).status_code == 200
    assert api.post("/v1/auth/refresh", json={"refresh_token": agency["tokens"]["refresh_token"]}).status_code == 401
    assert api.get("/v1/me", headers=successor).status_code == 401


def test_complete_intake_with_client_answers_and_human_confirmation(api, agency, brand_factory, service_factory, owner):
    brand = brand_factory(agency)
    bid = brand["brand"]["id"]
    actor = service_factory(brand)
    assert run_once(actor, fake_crawl)
    job = api.get(f"/v1/brands/{bid}/jobs/{brand['crawl_job_id']}", headers=agency["headers"])
    assert job.json()["status"] == "succeeded", job.text
    headers = invite_user(api, agency, brand, "client")
    answers = {"business_description": "A local service business.", "business_model": "Service fees", "audience_description": "India based business owners with an active hiring need.", "primary_objective": "Generate qualified enquiries", "budget_range": {"minimum": {"currency": "INR", "amount_paise": 1000000}, "maximum": {"currency": "INR", "amount_paise": 2000000}}, "target_geographies": ["India"], "languages": ["en", "hi"]}
    draft = api.put(f"/v1/brands/{bid}/onboarding", headers=headers, json={"expected_version": 0, "answers": answers})
    assert draft.status_code == 200, draft.text
    assert api.put(f"/v1/brands/{bid}/onboarding", headers=headers, json={"expected_version": 0, "answers": answers}).status_code == 409
    submitted = api.post(f"/v1/brands/{bid}/onboarding/submit", headers=headers, json={"expected_version": draft.json()["version"]})
    assert submitted.status_code == 200, submitted.text
    latest = api.get(f"/v1/brands/{bid}/profile", headers=agency["headers"]).json()
    confirmed = api.post(f"/v1/brands/{bid}/profile/confirm", headers=agency["headers"], json={"expected_profile_version": latest["version"], "onboarding_response_id": submitted.json()["id"]})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["approval_status"] == "confirmed"
    assert confirmed.json()["budget_range"]["minimum"]["amount_paise"] == 1000000
    assert api.get(f"/v1/brands/{bid}/workflow", headers=agency["headers"]).json()["state"] == "ready_for_research"
    assert api.put(f"/v1/brands/{bid}/onboarding", headers=headers, json={"expected_version": submitted.json()["version"], "answers": answers}).status_code == 409
    assert owner.execute("SELECT count(*) AS n FROM mi.tool_calls WHERE brand_id=%s", (bid,)).fetchone()["n"] == 1


def test_direct_sql_rls_and_cross_brand_foreign_keys(api, agency, brand_factory):
    first, second = brand_factory(agency), brand_factory(agency)
    headers = invite_user(api, agency, first, "client")
    uid = UUID(api.get("/v1/me", headers=headers).json()["id"])
    with transaction(uid) as c:
        assert c.execute("SELECT id FROM mi.brand_profiles").fetchall() == []
        assert {str(r["id"]) for r in c.execute("SELECT id FROM mi.brand_workspaces").fetchall()} == {first["brand"]["id"]}
        assert c.execute("SELECT id FROM mi.audit_logs").fetchall() == []
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT * FROM mi.integration_credentials")
    founder = UUID(api.get("/v1/me", headers=agency["headers"]).json()["id"])
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with transaction(founder) as c:
            other_org = uuid4()
            c.execute("INSERT INTO mi.onboarding_responses(organization_id,brand_id,respondent_id,version,status,answers) VALUES(%s,%s,%s,1,'draft',%s)", (other_org, first["brand"]["id"], founder, Jsonb({})))


def test_audit_is_append_only_and_integration_write_is_disabled(api, agency, brand_factory):
    brand = brand_factory(agency)
    founder = UUID(api.get("/v1/me", headers=agency["headers"]).json()["id"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with transaction(founder) as c:
            c.execute("UPDATE mi.audit_logs SET action='tampered' WHERE brand_id=%s", (brand["brand"]["id"],))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with transaction(founder) as c:
            c.execute("INSERT INTO mi.integrations(organization_id,brand_id,provider,status,access_mode) VALUES(%s,%s,'meta','connected','read')", (agency["organization"], brand["brand"]["id"]))


def test_no_actor_means_no_brands_or_private_password_access():
    with transaction() as c:
        assert c.execute("SELECT id FROM mi.brand_workspaces").fetchall() == []
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with transaction() as c:
            c.execute("SELECT password_hash FROM identity.users")


def test_audit_chain_verifies_and_all_brand_tables_force_rls(api, agency, brand_factory, owner):
    brand_factory(agency)
    owner.execute("SET LOCAL timezone='UTC'")
    records = owner.execute("SELECT a.*,entry_hash=encode(public.digest(convert_to((to_jsonb(a)-'entry_hash')::text,'UTF8'),'sha256'),'hex') AS verified FROM mi.audit_logs a WHERE organization_id=%s ORDER BY occurred_at,id", (agency["organization"],)).fetchall()
    assert records and all(row["verified"] for row in records)
    assert records[0]["previous_hash"] is None
    assert all(records[i]["previous_hash"] == records[i-1]["entry_hash"] for i in range(1, len(records)))
    tables = owner.execute("SELECT DISTINCT c.relname,c.relrowsecurity,c.relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid WHERE n.nspname='mi' AND c.relkind='r' AND a.attname='brand_id'").fetchall()
    assert len(tables) >= 29
    assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in tables)


def test_login_limit_and_logout_revoke_session(api, agency, owner):
    owner.execute("DELETE FROM identity.auth_attempts")
    owner.commit()
    for _ in range(5):
        result = api.post("/v1/auth/token", json={"email": agency["email"], "password": "incorrect-password"})
        assert result.status_code == 401
    assert api.post("/v1/auth/token", json={"email": agency["email"], "password": agency["password"]}).status_code == 429
    assert api.post("/v1/auth/logout", headers=agency["headers"]).status_code == 200
    assert api.get("/v1/me", headers=agency["headers"]).status_code == 401
