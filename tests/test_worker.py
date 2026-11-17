from concurrent.futures import ThreadPoolExecutor
from uuid import UUID
import pytest
from pydantic import ValidationError
from app.db import transaction
from app.worker import claim, complete, run_once
from app import worker
from tests.test_api_security import fake_crawl


def test_concurrent_claim_and_cancellation_fence(api, agency, brand_factory, service_factory):
    brand = brand_factory(agency)
    actor = service_factory(brand)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(actor), range(2)))
    claimed = [result for result in results if result]
    assert len(claimed) == 1
    bid = brand["brand"]["id"]
    workflow = api.get(f"/v1/brands/{bid}/workflow", headers=agency["headers"]).json()
    result = api.post(f"/v1/brands/{bid}/workflow/events", headers=agency["headers"], json={"event": "cancel", "expected_version": workflow["version"]})
    assert result.status_code == 200, result.text
    assert complete(actor, claimed[0], fake_crawl("https://example.com", 1), []) is False
    assert api.get(f"/v1/brands/{bid}/profile", headers=agency["headers"]).json()["version"] == 1


def test_second_invalid_extraction_requires_human_review(api, agency, brand_factory, service_factory, monkeypatch):
    brand = brand_factory(agency)
    actor = service_factory(brand)
    def invalid(*args):
        raise ValidationError.from_exception_data("BrandProfile", [{"type": "missing", "loc": ("name",), "input": {}}])
    monkeypatch.setattr(worker, "complete", invalid)
    assert run_once(actor, fake_crawl)
    bid = brand["brand"]["id"]
    with transaction(actor) as connection:
        connection.execute("UPDATE mi.jobs SET available_at=now() WHERE brand_id=%s AND status='retry'", (UUID(bid),))
    assert run_once(actor, fake_crawl)
    status = api.get(f"/v1/brands/{bid}/workflow", headers=agency["headers"]).json()
    assert status["state"] == "needs_human_review"
    assert status["validation_failures"] == 2
    job = api.get(f"/v1/brands/{bid}/jobs/{brand['crawl_job_id']}", headers=agency["headers"]).json()
    assert job["status"] == "failed"


def test_service_cannot_claim_another_brands_job(api, agency, brand_factory, service_factory):
    first, second = brand_factory(agency), brand_factory(agency)
    actor = service_factory(first)
    assert claim(actor)["brand_id"] == UUID(first["brand"]["id"])
    assert claim(actor) is None


def test_service_database_login_cannot_impersonate_founder_or_read_auth(api, agency, brand_factory, service_factory):
    import psycopg
    from app.config import settings
    first, second = brand_factory(agency), brand_factory(agency)
    actor = service_factory(first)
    founder = UUID(api.get("/v1/me", headers=agency["headers"]).json()["id"])
    with transaction(founder, settings().worker_database_url.get_secret_value()) as connection:
        assert connection.execute("SELECT mi.actor_id() AS id").fetchone()["id"] == actor
        assert {str(row["id"]) for row in connection.execute("SELECT id FROM mi.brand_workspaces").fetchall()} == {first["brand"]["id"]}
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with transaction(actor, settings().worker_database_url.get_secret_value()) as connection:
            connection.execute("SELECT * FROM identity.login_lookup('someone@example.com')")
