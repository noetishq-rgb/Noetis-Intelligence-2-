"""Explicit, single-generation research pilot. No scheduled or automatic reruns."""
from decimal import Decimal
import re
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Literal

from app.config import settings
from app.crawl_scheduler import verify_scheduler
from app.db import transaction, check_runtime_role
from app.repository import latest_profile, workflow_for_brand, lock_key
from app.services.research_budget import BudgetError
from app.worker import validate_service

router = APIRouter()
MODEL = 'claude-sonnet-4-6'
# Standard uncached first-party rates, checked 2026-11-17 against Anthropic pricing.
INPUT_USD_PER_MILLION = Decimal('3')
OUTPUT_USD_PER_MILLION = Decimal('15')
BUDGET_USD = Decimal('1')
MAX_INPUT = 16000
MAX_OUTPUT = 4096


def dollars(input_tokens, output_tokens):
    return (Decimal(input_tokens) * INPUT_USD_PER_MILLION +
            Decimal(output_tokens) * OUTPUT_USD_PER_MILLION) / 1000000


def saved_source(page):
    from app.research_models import SourceSeed
    from app.services.research_sources import CollectedSource
    text = page['text_excerpt']
    match = re.search(r'\bIndia(?:n)?\b', text, re.IGNORECASE)
    # Keep a verbatim window around explicit regional context instead of sending
    # navigation boilerplate. Never infer geography from the domain suffix.
    if match and match.start() > 1200:
        text = text[max(0,match.start()-600):match.start()+1800]
    seed = SourceSeed(url=page['url'], area='brand', geographic_scope='india' if match else 'unknown')
    source = CollectedSource(seed,page['url'],page['title'],None,text,page['content_sha256'],.85,
        ['Previously captured public HTML; website statements are not independent verification.',
         'Regional relevance is based only on explicit website text; this is not independent market evidence.',
         'Website-only pilot: no independent audience or competitor research.'],page['accessed_at'])
    return seed,source


def admit_pilot(connection, job, request, padded_input_count, model):
    if model != MODEL or request.maximum_input_tokens > MAX_INPUT or request.maximum_output_tokens > MAX_OUTPUT:
        raise BudgetError('pilot_model_or_token_limits_changed')
    if dollars(padded_input_count, request.maximum_output_tokens) > BUDGET_USD:
        raise BudgetError('pilot_dollar_budget_exceeded')
    attempted = connection.execute('SELECT count(*) AS n FROM mi.model_dispatches WHERE brand_id=%s',
                                   (job['brand_id'],)).fetchone()['n']
    if attempted:
        raise BudgetError('pilot_generation_already_attempted')


def binding():
    config = settings()
    if not (config.research_pilot_enabled and config.research_pilot_brand_id and
            config.research_pilot_service_user_id and config.research_pilot_worker_database_url):
        raise HTTPException(503, 'Research pilot worker is not configured')
    actor = config.research_pilot_service_user_id
    url = config.research_pilot_worker_database_url.get_secret_value()
    with transaction(actor, url) as c:
        check_runtime_role(c)
        validate_service(c, actor)
        brands = {r['id'] for r in c.execute('SELECT id FROM mi.brand_workspaces').fetchall()}
        if brands != {config.research_pilot_brand_id}:
            raise HTTPException(503, 'Research worker brand binding is unsafe')
        if not c.execute("SELECT mi.has_brand_scope(%s,'research:run') AS ok", (config.research_pilot_brand_id,)).fetchone()['ok']:
            raise HTTPException(503, 'Research service scope is missing')
    return config, actor, url


def readiness(c, brand_id):
    profile = latest_profile(c, brand_id)
    workflow = workflow_for_brand(c, brand_id)
    missing = [g['field'] for g in (profile['profile']['missing_inputs'] if profile else []) if g['critical']]
    captured = c.execute('SELECT count(*) AS n FROM mi.crawl_pages WHERE brand_id=%s', (brand_id,)).fetchone()['n']
    ready = bool(profile and not missing and profile['status'] == 'confirmed' and workflow and
                 workflow['state'] == 'ready_for_research' and workflow['approved_profile_id'] == profile['artifact_id'] and captured)
    job = c.execute("SELECT id,status,last_error FROM mi.jobs WHERE brand_id=%s AND kind='research' ORDER BY created_at DESC LIMIT 1", (brand_id,)).fetchone()
    usage = c.execute("SELECT coalesce(sum(input_tokens),0) AS input_tokens,coalesce(sum(output_tokens),0) AS output_tokens FROM mi.prompt_runs WHERE brand_id=%s AND body->>'model'=%s", (brand_id,MODEL)).fetchone()
    dispatches = c.execute('SELECT count(*) AS n FROM mi.model_dispatches WHERE brand_id=%s', (brand_id,)).fetchone()['n']
    uncertain = c.execute("SELECT count(*) AS n FROM mi.model_dispatches WHERE brand_id=%s AND state IN('outcome_unknown','dispatched')", (brand_id,)).fetchone()['n']
    return {'ready': ready, 'missing_required_inputs': missing, 'profile_confirmed': bool(profile and profile['status'] == 'confirmed'),
            'workflow_state': workflow['state'] if workflow else None, 'source_pages': captured, 'job': job,
            'generation_attempts': dispatches, 'input_tokens': usage['input_tokens'], 'output_tokens': usage['output_tokens'],
            'uncertain_dispatches': uncertain,
            'recorded_token_cost_usd': str(dollars(usage['input_tokens'], usage['output_tokens'])),
            'budget_usd': str(BUDGET_USD), 'model': MODEL, 'automatic_reruns': False}


def check_model_access(config):
    if not config.anthropic_api_key or config.anthropic_model != MODEL:
        return {'verified': False, 'error': 'pilot_model_not_configured'}
    # Model metadata is not a generation. Never return provider response bodies or headers.
    try:
        with httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False, trust_env=False) as client:
            r = client.get('https://api.anthropic.com/v1/models/' + MODEL, headers={
                'x-api-key': config.anthropic_api_key.get_secret_value(), 'anthropic-version': config.anthropic_version})
        if r.status_code != 200:
            return {'verified': False, 'error': 'model_metadata_http_' + str(r.status_code)}
        return {'verified': r.json().get('id') == MODEL}
    except (httpx.HTTPError, ValueError):
        return {'verified': False, 'error': 'model_metadata_unavailable'}


def run_pilot(action, *, capture_job_id=None):
    config, actor, url = binding()
    brand_id = config.research_pilot_brand_id
    with transaction(actor, url) as c:
        status = readiness(c, brand_id)
    access = check_model_access(config)
    if action == 'status' or not status['ready'] or status['job'] or not access['verified']:
        return {**status, 'model_access': access, 'processed': False}
    from app.research_api import start
    from app.research_models import ResearchStart
    from app.research_worker import run_once
    with transaction(actor, url) as c:
        lock_key(c, 'research-pilot:' + str(brand_id))
        state = readiness(c, brand_id)
        if not state['ready'] or state['job'] or state['generation_attempts']:
            return {**state, 'model_access': access, 'processed': False}
        workflow = workflow_for_brand(c, brand_id, lock=True)
        if capture_job_id is None:
            latest = c.execute("""SELECT id FROM mi.jobs WHERE brand_id=%s AND workflow_id=%s
                AND kind='website_crawl' AND status='succeeded' ORDER BY created_at DESC,id DESC LIMIT 1""",
                (brand_id, workflow['id'])).fetchone()
            capture_job_id = latest['id'] if latest else None
        pages = c.execute('''SELECT p.*,s.accessed_at FROM mi.crawl_pages p JOIN mi.research_sources s
            ON s.id=p.source_id AND s.brand_id=p.brand_id JOIN mi.jobs j ON j.id=p.job_id AND j.brand_id=p.brand_id
            WHERE p.brand_id=%s AND j.id=%s AND j.workflow_id=%s AND j.kind='website_crawl'
            AND j.status='succeeded' ORDER BY p.created_at,p.id LIMIT 5''',
            (brand_id, capture_job_id, workflow['id'])).fetchall()
        if not pages:
            raise HTTPException(409, 'Successful saved website capture is required')
        snapshots = [saved_source(p) for p in pages]
        body = ResearchStart(expected_workflow_version=workflow['version'], sources=[seed for seed,_ in snapshots])
        accepted = start(brand_id, body, 'single-dollar-research-pilot-v1', {'id': actor}, c)
        c.execute('UPDATE mi.jobs SET max_attempts=1 WHERE id=%s AND brand_id=%s', (accepted.job_id, brand_id))
    def saved_capture(seeds, queries, trace=None):
        return [source for _,source in snapshots], ['Only saved brand website pages were used; no paid search or social APIs were called.']
    worked = run_once(actor, collector=saved_capture, database_url=url)
    with transaction(actor, url) as c:
        return {**readiness(c, brand_id), 'model_access': access, 'processed': worked}


class PilotAction(BaseModel):
    action: Literal['status', 'run'] = 'status'


@router.post('/internal/research-pilot', include_in_schema=False)
def pilot(body: PilotAction, claims=Depends(verify_scheduler)):
    if claims['event_name'] != 'workflow_dispatch':
        raise HTTPException(403, 'Research pilot requires an explicit workflow dispatch')
    return run_pilot(body.action)
