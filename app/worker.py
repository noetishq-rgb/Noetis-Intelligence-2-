"""Durable PostgreSQL queue. Run one process per narrow service principal."""
import argparse
from contextvars import ContextVar
from datetime import datetime, timezone
import logging
import time
from uuid import UUID, uuid4
from pydantic import ValidationError
from app.config import settings
from app.db import transaction as database_transaction
from app.repository import latest_profile, lock_key, persist_profile, transition, workflow_for_brand
from app.services.crawl import CrawlError, crawl
from app.services.extraction import extract_profile
from app.workflow import GateContext

logger = logging.getLogger("marketing_intelligence.worker")
_bound_database_url = ContextVar('crawl_database_url',default=None)


def transaction(actor):
    config = settings()
    override = _bound_database_url.get()
    url = config.worker_database_url or config.database_url
    return database_transaction(actor, override or url.get_secret_value())


def validate_service(connection, actor):
    bound = connection.execute("SELECT mi.is_bound_service() AS bound,mi.actor_id() AS actor").fetchone()
    if not bound["bound"] or bound["actor"] != actor:
        raise RuntimeError("Worker database login must be bound to its service identity")
    user = connection.execute("SELECT is_service,disabled FROM identity.users WHERE id=%s", (actor,)).fetchone()
    if not user or not user["is_service"] or user["disabled"]:
        raise RuntimeError("Worker requires an enabled service account")


def claim(actor):
    with transaction(actor) as connection:
        validate_service(connection, actor)
        candidates = connection.execute("""SELECT id,workflow_id,brand_id FROM mi.jobs WHERE
            kind='website_crawl' AND ((status IN('queued','retry') AND available_at<=now()) OR (status='running' AND lease_expires_at<now()))
            ORDER BY available_at,id LIMIT 16""").fetchall()
        for candidate in candidates:
            workflow = connection.execute("SELECT * FROM mi.workflow_instances WHERE id=%s AND brand_id=%s FOR UPDATE SKIP LOCKED", (candidate["workflow_id"], candidate["brand_id"])).fetchone()
            if not workflow:
                continue
            job = connection.execute("SELECT * FROM mi.jobs WHERE id=%s AND brand_id=%s FOR UPDATE SKIP LOCKED", (candidate["id"], candidate["brand_id"])).fetchone()
            if not job or job["status"] not in {"queued", "retry", "running"}:
                continue
            if job["status"] == "running" and job["lease_expires_at"] >= datetime.now(timezone.utc):
                continue
            if workflow["state"] not in {"crawl_queued", "crawling"}:
                connection.execute("UPDATE mi.jobs SET status='cancelled',last_error='workflow_not_crawlable' WHERE id=%s AND brand_id=%s", (job["id"], job["brand_id"]))
                continue
            if job["attempts"] >= job["max_attempts"]:
                connection.execute("UPDATE mi.jobs SET status='failed',last_error='lease_recovery_exhausted',lease_id=NULL,lease_expires_at=NULL WHERE id=%s AND brand_id=%s", (job["id"], job["brand_id"]))
                if workflow["state"] == "crawling":
                    transition(connection, workflow, "crawl_failed", GateContext(), last_error="lease_recovery_exhausted")
                continue
            lease = uuid4()
            job = connection.execute("UPDATE mi.jobs SET status='running',attempts=attempts+1,lease_id=%s,lease_expires_at=now()+interval '120 seconds' WHERE id=%s AND brand_id=%s RETURNING *", (lease, job["id"], job["brand_id"])).fetchone()
            if workflow["state"] == "crawl_queued":
                transition(connection, workflow, "start_crawl", GateContext())
            return job
    return None


def locked_job(connection, job):
    row = connection.execute("SELECT * FROM mi.jobs WHERE id=%s AND brand_id=%s FOR UPDATE", (job["id"], job["brand_id"])).fetchone()
    if not row or row["status"] != "running" or row["lease_id"] != job["lease_id"] or row["lease_expires_at"] <= datetime.now(timezone.utc):
        return None
    return row


def save_traces(connection, job, actor, traces):
    for trace in traces:
        connection.execute("INSERT INTO mi.tool_calls(organization_id,brand_id,job_id,actor_id,endpoint,method,response_status,error_code,latency_ms,attempt,lease_id,request_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (job["organization_id"], job["brand_id"], job["id"], actor, trace["endpoint"], trace.get("method","GET"), trace["response_status"], trace["error_code"], trace["latency_ms"], job["attempts"], job["lease_id"], trace.get("request_sha256")))


def complete(actor, job, pages, traces):
    with transaction(actor) as connection:
        lock_key(connection, "profile:" + str(job["brand_id"]))
        workflow = workflow_for_brand(connection, job["brand_id"], lock=True)
        if not locked_job(connection, job):
            save_traces(connection, job, actor, traces)
            return False
        brand = connection.execute("SELECT * FROM mi.brand_workspaces WHERE id=%s AND organization_id=%s", (job["brand_id"], job["organization_id"])).fetchone()
        latest = latest_profile(connection, job["brand_id"])
        sourced_pages = [(page, uuid4()) for page in pages]
        profile = extract_profile(job["brand_id"], latest["version"] + 1 if latest else 1, brand["intake"], sourced_pages, UUID(job["payload"]["input_source_id"]))
        for page, source_id in sourced_pages:
            connection.execute("INSERT INTO mi.research_sources(id,organization_id,brand_id,url,publisher,accessed_at,geographic_scope,provenance,content_sha256) VALUES(%s,%s,%s,%s,%s,now(),'unknown','website',%s)",
                (source_id, job["organization_id"], job["brand_id"], page.url, page.title, page.sha256))
            connection.execute("INSERT INTO mi.crawl_pages(organization_id,brand_id,job_id,source_id,url,http_status,title,description,text_excerpt,content_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (job["organization_id"], job["brand_id"], job["id"], source_id, page.url, page.status, page.title, page.description, page.excerpt, page.sha256))
        persist_profile(connection, brand, profile, actor)
        save_traces(connection, job, actor, traces)
        connection.execute("UPDATE mi.jobs SET status='succeeded',lease_id=NULL,lease_expires_at=NULL,last_error=NULL WHERE id=%s AND brand_id=%s", (job["id"], job["brand_id"]))
        transition(connection, workflow, "crawl_succeeded", GateContext(), last_error=None)
    return True


def fail(actor, job, code, traces, validation=False):
    transient = code in {"fetch_failed", "dns_failed", "robots_unavailable"}
    with transaction(actor) as connection:
        workflow = workflow_for_brand(connection, job["brand_id"], lock=True)
        row = locked_job(connection, job)
        if not row:
            save_traces(connection, job, actor, traces)
            return False
        save_traces(connection, job, actor, traces)
        if validation:
            workflow = transition(connection, workflow, "validation_failed", GateContext(validation_failures=workflow["validation_failures"]), last_error=code)
            retry = workflow["state"] != "needs_human_review" and row["attempts"] < row["max_attempts"]
            if not retry and workflow["state"] == "crawling":
                transition(connection, workflow, "crawl_failed", GateContext(), last_error=code)
        else:
            retry = transient and row["attempts"] < row["max_attempts"]
            if not retry:
                transition(connection, workflow, "crawl_failed", GateContext(), last_error=code)
        connection.execute("UPDATE mi.jobs SET status=%s,last_error=%s,validation_failures=least(2,validation_failures+%s),available_at=now()+make_interval(secs=>%s),lease_id=NULL,lease_expires_at=NULL WHERE id=%s AND brand_id=%s",
            ("retry" if retry else "failed", code, int(validation), min(60, 2 ** row["attempts"]), job["id"], job["brand_id"]))
    return True


def run_once(actor: UUID, crawler=crawl, *, database_url: str | None = None) -> bool:
    token = _bound_database_url.set(database_url)
    try:
        return _run_once(actor,crawler)
    finally:
        _bound_database_url.reset(token)


def _run_once(actor: UUID, crawler=crawl) -> bool:
    job = claim(actor)
    if job is None:
        return False
    traces = []
    try:
        pages = crawler(job["payload"]["url"], job["payload"]["max_pages"], trace=traces.append)
        complete(actor, job, pages, traces)
    except CrawlError as exc:
        fail(actor, job, str(exc), traces)
    except ValidationError:
        fail(actor, job, "profile_validation_failed", traces, validation=True)
    except Exception:
        logger.error("job_interrupted job_id=%s brand_id=%s", job["id"], job["brand_id"])
        raise
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not settings().service_user_id:
        raise SystemExit("MI_SERVICE_USER_ID is required")
    actor = UUID(settings().service_user_id)
    while True:
        had_work = run_once(actor)
        if args.once:
            break
        if not had_work:
            time.sleep(2)


if __name__ == "__main__":
    main()
