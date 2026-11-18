"""Authenticated Brand Brain routes composed over existing brand/intake/research APIs."""
from copy import deepcopy
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException, Header, Query
from psycopg.types.json import Jsonb
from app.auth import actor_transaction,current_user,require_brand,require_admin
from app.brain_models import SetupDraft,SessionSave,VersionAction,PilotRunRequest,CaptureStart,FindingReview,ThreadCreate,CommentCreate,ThreadAction,BrainConfirm,CHAPTERS
from app.repository import lock_key,payload_hash,latest_profile,workflow_for_brand
from app.services.brain import synthesise,version_row,project,quality,threads
from app.config import settings
from app.crawl_scheduler import capture_binding
from app.services.crawl import CrawlError, validate_url

router=APIRouter(prefix='/v1/brands/{brand_id}/brain',tags=['Brand Brain'])


def access(c,bid,write=False):
    brand=require_brand(c,bid,'onboarding:write' if write else 'onboarding:read')
    if not c.execute('SELECT mi.brain_write(%s) AS ok' if write else 'SELECT mi.brain_read(%s) AS ok',(bid,)).fetchone()['ok']:
        raise HTTPException(404,'Brand workspace not found')
    return brand


def session(c,bid,sid=None):
    row=c.execute('SELECT * FROM mi.brand_onboarding_sessions WHERE brand_id=%s'+(' AND id=%s' if sid else '')+' ORDER BY created_at DESC LIMIT 1',(bid,sid) if sid else (bid,)).fetchone()
    if not row:raise HTTPException(404,'Onboarding session not found')
    return row


def mutable(c,bid,vid):
    lock_key(c,'brain:'+str(bid))
    row=version_row(c,bid,vid)
    latest=version_row(c,bid)
    if latest['id']!=vid:raise HTTPException(409,'A newer Brand Brain exists; reload before editing')
    if c.execute('SELECT id FROM mi.brand_brain_confirmations WHERE brand_id=%s AND version_id=%s',(bid,vid)).fetchone():
        raise HTTPException(409,'Confirmed versions are immutable; create a new draft to make changes')
    return row


@router.post('/sessions',status_code=201)
def create_session(brand_id:UUID,body:SetupDraft,user=Depends(current_user),connection=Depends(actor_transaction)):
    b=access(connection,brand_id,True);lock_key(connection,'brain:'+str(brand_id))
    current=connection.execute("SELECT * FROM mi.brand_onboarding_sessions WHERE brand_id=%s AND status='draft' ORDER BY created_at DESC LIMIT 1",(brand_id,)).fetchone()
    if current:return current
    job=connection.execute("SELECT id FROM mi.jobs WHERE brand_id=%s AND kind='website_crawl' ORDER BY created_at DESC LIMIT 1",(brand_id,)).fetchone() if bscope(connection,brand_id,'internal:read') else None
    return connection.execute('INSERT INTO mi.brand_onboarding_sessions(organization_id,brand_id,draft,crawl_job_id,created_by) VALUES(%s,%s,%s,%s,%s) RETURNING *',(b['organization_id'],brand_id,Jsonb(body.model_dump(mode='json')),job['id'] if job else None,user['id'])).fetchone()


@router.post('/sessions/{session_id}/complete-intake', status_code=201)
def complete_legacy_intake(brand_id: UUID, session_id: UUID, body: VersionAction,
                           user=Depends(current_user), connection=Depends(actor_transaction)):
    brand = access(connection, brand_id, True)
    lock_key(connection, 'brain:' + str(brand_id))
    current = session(connection, brand_id)
    if current['id'] != session_id or current['version'] != body.expected_version:
        raise HTTPException(409, 'A newer intake exists. Reload to continue.')
    draft = SetupDraft.model_validate(current['draft'])
    if current['status'] != 'submitted' or draft.research_intake_version != 0 or current['research_job_id']:
        raise HTTPException(409, 'Only a submitted legacy intake awaiting research can be completed')
    draft.research_intake_version = 1
    return connection.execute(
        """INSERT INTO mi.brand_onboarding_sessions
           (organization_id, brand_id, draft, crawl_job_id, created_by)
           VALUES (%s,%s,%s,%s,%s) RETURNING *""",
        (brand['organization_id'], brand_id, Jsonb(draft.model_dump(mode='json')),
         current['crawl_job_id'], user['id'])
    ).fetchone()


@router.get('/sessions/current')
def current_session(brand_id:UUID,connection=Depends(actor_transaction)):
    access(connection,brand_id);return session(connection,brand_id)


@router.put('/sessions/{session_id}')
def save_session(brand_id:UUID,session_id:UUID,body:SessionSave,user=Depends(current_user),connection=Depends(actor_transaction)):
    access(connection,brand_id,True);lock_key(connection,'brain:'+str(brand_id));old=session(connection,brand_id,session_id)
    if old['version']!=body.expected_version:raise HTTPException(409,'Draft changed; reload before saving')
    if old['status']!='draft':raise HTTPException(409,'Submitted inputs are immutable; start a new session')
    return connection.execute('UPDATE mi.brand_onboarding_sessions SET draft=%s,version=version+1,updated_at=now() WHERE id=%s AND brand_id=%s RETURNING *',(Jsonb(body.draft.model_dump(mode='json')),session_id,brand_id)).fetchone()


@router.post('/sessions/{session_id}/submit')
def submit(brand_id:UUID,session_id:UUID,body:VersionAction,user=Depends(current_user),connection=Depends(actor_transaction)):
    b=access(connection,brand_id,True);lock_key(connection,'brain:'+str(brand_id));old=session(connection,brand_id,session_id)
    if old['version']!=body.expected_version:raise HTTPException(409,'Draft changed; reload before submitting')
    if old['status']=='submitted':return old
    try:details=SetupDraft.model_validate(old['draft']).require_complete()
    except ValueError as exc:raise HTTPException(422,str(exc))
    if details.research_intake_version == 1 and old['crawl_job_id']:
        captured_site = b['website_url']
        submitted_site = next((p.url for p in details.online_presence if p.platform == 'website'), None)
        if not captured_site or not submitted_site or captured_site.rstrip('/') != submitted_site.rstrip('/'):
            raise HTTPException(409, 'The website changed. Use the website linked to this capture before submitting.')
    # Owner-issued empty workspaces accept client inputs before provider setup.
    if b['intake'].get('client_onboarding'):
        connection.execute('SELECT mi.submit_client_setup(%s,%s)',(brand_id,Jsonb(old['draft'])))
    elif not old['crawl_job_id']:
        raise HTTPException(409,'An authorized team member must start the existing public-capture workflow')
    response_id=None
    if details.research_intake_version==1:
        previous=connection.execute('SELECT coalesce(max(version),0) AS version FROM mi.onboarding_responses WHERE brand_id=%s AND respondent_id=%s',(brand_id,user['id'])).fetchone()['version']
        response_id=connection.execute("INSERT INTO mi.onboarding_responses(organization_id,brand_id,respondent_id,version,status,answers,submitted_at) VALUES(%s,%s,%s,%s,'submitted',%s,now()) RETURNING id",(b['organization_id'],brand_id,user['id'],previous+1,Jsonb(details.research_answers().model_dump(mode='json')))).fetchone()['id']
    return connection.execute("UPDATE mi.brand_onboarding_sessions SET status='submitted',onboarding_response_id=%s,version=version+1,updated_at=now() WHERE id=%s AND brand_id=%s RETURNING *",(response_id,session_id,brand_id)).fetchone()


@router.post('/sessions/{session_id}/confirm-intake')
def confirm_intake(brand_id:UUID,session_id:UUID,body:VersionAction,user=Depends(current_user),connection=Depends(actor_transaction)):
    from app.main import confirm_profile
    from app.api_types import ProfileConfirm
    b=access(connection,brand_id,True);require_admin(connection,b['organization_id'])
    lock_key(connection,'brain:'+str(brand_id));s=session(connection,brand_id,session_id)
    if s['version']!=body.expected_version or s['status']!='submitted' or session(connection,brand_id)['id']!=session_id:
        raise HTTPException(409,'Review the current submitted intake before confirming')
    details=SetupDraft.model_validate(s['draft'])
    if details.research_intake_version!=1:
        raise HTTPException(409,'Complete the detailed research form first')
    try:details.require_complete()
    except ValueError as exc:raise HTTPException(422,str(exc))
    job=connection.execute("SELECT status FROM mi.jobs WHERE id=%s AND brand_id=%s AND kind='website_crawl'",(s['crawl_job_id'],brand_id)).fetchone()
    if not job or job['status']!='succeeded':raise HTTPException(409,'Complete public website capture before reviewing the profile')
    profile=latest_profile(connection,brand_id)
    if not profile:raise HTTPException(409,'A captured draft profile is required')
    if profile and profile['status']=='confirmed':return profile['profile']
    response=connection.execute("SELECT id FROM mi.onboarding_responses WHERE brand_id=%s AND id=%s AND status='submitted' AND answers=%s",(brand_id,s['onboarding_response_id'],Jsonb(details.research_answers().model_dump(mode='json')))).fetchone()
    if not response:raise HTTPException(409,'A matching submitted research intake is required')
    connection.execute("SELECT set_config('app.onboarding_response_id',%s,true)",(str(response['id']),))
    connection.execute('SELECT mi.submit_intake(%s)',(brand_id,))
    return confirm_profile(brand_id,ProfileConfirm(expected_profile_version=profile['version'],onboarding_response_id=response['id']),user,connection)


@router.post('/sessions/{session_id}/research-pilot')
def run_session_pilot(brand_id:UUID,session_id:UUID,body:PilotRunRequest,user=Depends(current_user)):
    from app.db import transaction
    from app.research_pilot import run_pilot
    with transaction(user['id']) as c:
        b=access(c,brand_id,True);require_admin(c,b['organization_id']);s=session(c,brand_id,session_id)
        if s['version']!=body.expected_version or s['status']!='submitted' or session(c,brand_id)['id']!=session_id:
            raise HTTPException(409,'Use the current submitted intake')
        if not s['crawl_job_id']:
            raise HTTPException(409, 'Complete website capture for this intake before research')
        if settings().research_pilot_brand_id!=brand_id:
            raise HTTPException(503,'This brand does not have a configured research pilot')
        # Older captures predate the prompt template. Only an organization
        # admin can install it; the research worker remains read-only here.
        from app.services.research_storage import ensure_template
        lock_key(c, 'brain:' + str(brand_id))
        ensure_template(c, b, user['id'], 'brand_research')
    # The caller transaction is closed before the bound worker writes audit rows.
    try:
        brief = dict(s['draft'].get('brief') or {})
        if body.question:
            brief['question'] = body.question
        result=run_pilot('run', capture_job_id=s['crawl_job_id'], brief=brief or None)
    finally:
        with transaction(user['id']) as c:
            job=c.execute("SELECT id FROM mi.jobs WHERE brand_id=%s AND kind='research' ORDER BY created_at DESC LIMIT 1",(brand_id,)).fetchone()
            if job:
                c.execute('UPDATE mi.brand_onboarding_sessions SET research_job_id=%s,version=version+1,updated_at=now() WHERE id=%s AND brand_id=%s AND research_job_id IS DISTINCT FROM %s',(job['id'],session_id,brand_id,job['id']))
    with transaction(user['id']) as c:
        result['session']=session(c,brand_id,session_id)
    return result


@router.get('/sessions/{session_id}/progress')
def progress(brand_id:UUID,session_id:UUID,connection=Depends(actor_transaction)):
    b=access(connection,brand_id);s=session(connection,brand_id,session_id)
    details=SetupDraft.model_validate(s['draft']);missing=details.missing_research_inputs()
    internal=bscope(connection,brand_id,'internal:read')
    profile=latest_profile(connection,brand_id) if internal else None
    job=connection.execute('SELECT mi.brain_job_status(%s,%s) AS job',(brand_id,s['crawl_job_id'])).fetchone()['job'] if s['crawl_job_id'] else None
    capture_unconfigured = (settings().crawl_scheduler_enabled and not capture_binding(brand_id)
                            and (not job or job['status'] in {'queued','retry'}))
    # Broader research remains the existing human/provider-gated workflow. Reflect it if one exists.
    research=connection.execute('SELECT mi.brain_job_status(%s,%s) AS job',(brand_id,s['research_job_id'])).fetchone()['job'] if s['research_job_id'] else None
    question = None
    if internal and research:
        row = connection.execute("SELECT payload->'brief'->>'question' AS question FROM mi.jobs WHERE id=%s AND brand_id=%s",
                                 (s['research_job_id'],brand_id)).fetchone()
        question = row['question'] if row else None
    v=connection.execute('SELECT * FROM mi.brand_brain_versions WHERE brand_id=%s AND session_id=%s ORDER BY version DESC LIMIT 1',(brand_id,s['id'])).fetchone()
    done=bool(job and job['status']=='succeeded');failed=bool(job and job['status']=='failed');research_done=bool(research and research['status']=='succeeded');research_failed=bool(research and research['status']=='failed')
    coverage = {}
    if research_done and internal:
        row = connection.execute(
            "SELECT body->'coverage' AS areas FROM mi.research_dossiers WHERE brand_id=%s AND job_id=%s",
            (brand_id, s['research_job_id'])
        ).fetchone()
        coverage = {item['area']: item['status'] for item in (row or {}).get('areas') or []}
    def research_stage(area):
        if research_done:
            return 'completed' if coverage.get(area) == 'supported' else 'needs_review'
        if research_failed:
            return 'failed'
        if research and research['status'] == 'running':
            return 'running'
        return 'needs_review' if done else 'pending'

    stage1='completed' if done else 'failed' if failed else 'running' if job and job['status']=='running' else 'pending'
    stages=[{'key':'presence','title':'Discovering online presence','state':stage1},
        {'key':'business','title':'Understanding the business','state':'completed' if done else 'pending'},
        {'key':'products','title':'Exploring products and offerings','state':'needs_review' if done else 'pending'},
        {'key':'audience','title':'Researching the audience','state':research_stage('audience')},
        {'key':'market','title':'Analysing market context','state':research_stage('market')},
        {'key':'synthesis','title':'Synthesising the Brand Brain','state':'completed' if v else 'pending'}]
    sources=v['body']['sources'] if v else []
    # Before synthesis, only staff may query raw source storage; clients get safe materialized metadata later.
    if not v and bscope(connection,brand_id,'internal:read'):
        rows=connection.execute('SELECT source_id,url,title,created_at FROM mi.crawl_pages WHERE brand_id=%s AND job_id=%s ORDER BY created_at',(brand_id,s['crawl_job_id'])).fetchall()
        sources=[{'source_id':r['source_id'],'url':r['url'],'publisher':r['title'],'accessed_at':r['created_at'],'access_limitations':['Public HTML; underlying website claims require review.']} for r in rows]
    waiting_configuration=s['status']=='submitted' and not job
    config = settings()
    pilot_configured = bool(config.research_pilot_enabled and config.research_pilot_brand_id == brand_id
                            and config.research_pilot_worker_database_url and config.research_pilot_service_user_id)
    state='created' if s['status']=='draft' and (not job or not b['intake'].get('client_onboarding')) else 'awaiting_configuration' if waiting_configuration else 'failed' if failed or research_failed else 'completed' if v and research_done else 'awaiting_input' if done else 'running' if job and job['status']=='running' else 'cancelled' if job and job['status']=='cancelled' else 'queued'
    return {'session_id':s['id'],'state':'capture_unconfigured' if capture_unconfigured else state,'capture_job':job,'research_job':research,'stages':stages,'sources':sources,'source_count':len(sources),'version_id':v['id'] if v else None,
        'research_question':question,'research_pilot_configured':pilot_configured,'missing_research_inputs':missing,'profile_confirmed':profile['status']=='confirmed' if profile else None,
        'pending_action':'configure_capture' if capture_unconfigured else 'complete_research_inputs' if missing else 'capture_website' if not done else 'review_profile' if not research and not (profile and profile['status']=='confirmed') else 'configure_research' if not research and not pilot_configured else 'start_research' if not research else 'review_research' if research_done else 'research_job',
        'can_synthesise':done and s['status']=='submitted' and bscope(connection,brand_id,'internal:read'),'can_retry':failed and bscope(connection,brand_id,'crawl:run'),
        'capture_unconfigured': bool(capture_unconfigured),
        'activity':'Your agency needs to connect a website capture worker to this brand.' if capture_unconfigured else 'Your brand details are saved. Your agency can review them now; research and API connections will be configured separately.' if waiting_configuration else 'The research job failed. Review the recorded error and existing workflow gates before requesting another run.' if research_failed else 'The existing research worker is running; its output remains unconfirmed.' if research and research['status']=='running' else 'Captured public pages are ready for review. Audience and market research require the existing confirmed intake, provider and explicit research start.' if done else 'Waiting for the existing public-capture worker.' if not failed else 'Public capture failed; review access limitations before retrying.','error':research['last_error'] if research_failed else job['last_error'] if job else None}


def bscope(c,bid,scope):return c.execute('SELECT mi.has_brand_scope(%s,%s) AS ok',(bid,scope)).fetchone()['ok']


@router.post('/sessions/{session_id}/capture',status_code=202)
def start_capture(brand_id:UUID,session_id:UUID,body:CaptureStart,idempotency_key:str=Header(...),user=Depends(current_user),connection=Depends(actor_transaction)):
    """Bridge an owner-issued workspace to the existing crawl queue; no model calls."""
    from app.main import start_crawl,validate_idempotency
    from app.models import CrawlRequest
    b=access(connection,brand_id,True);require_admin(connection,b['organization_id'])
    validate_idempotency(idempotency_key);lock_key(connection,'brain:'+str(brand_id))
    s=session(connection,brand_id,session_id)
    key=payload_hash({'session_id':str(session_id),'key':idempotency_key})
    previous=connection.execute("SELECT * FROM mi.idempotency_keys WHERE brand_id=%s AND actor_id=%s AND operation='crawl' AND key=%s",(brand_id,user['id'],key)).fetchone()
    draft=deepcopy(s['draft'])
    supplied=str(body.website_url) if body.website_url else None
    site=next((link['url'] for link in draft['online_presence'] if link['platform']=='website'),None)
    site=supplied or site or b['website_url']
    if not site:raise HTTPException(422,'Add the official website URL before starting public capture')
    try:validate_url(site)
    except CrawlError:raise HTTPException(422,'Use a public HTTP or HTTPS website without credentials, fragments or nonstandard ports')
    if previous:
        previous_job=connection.execute('SELECT * FROM mi.jobs WHERE id=%s AND brand_id=%s',(previous['response']['id'],brand_id)).fetchone()
        if not previous_job or str(s['crawl_job_id'])!=str(previous_job['id']) or previous['request_hash']!=payload_hash({'max_pages':body.max_pages}) or previous_job['payload']['url'].rstrip('/')!=site.rstrip('/'):
            raise HTTPException(409,'Idempotency key already used with different capture inputs')
        return {'session':s,'job':previous['response']}
    if s['version']!=body.expected_version:raise HTTPException(409,'Draft changed; reload before starting capture')
    if s['crawl_job_id']:raise HTTPException(409,'Public capture is already linked to this session; open its status')
    if settings().crawl_scheduler_enabled and not capture_binding(brand_id):
        raise HTTPException(503, 'The agency needs to configure website capture for this brand before queuing a job')
    submitted_site=next((link['url'] for link in draft['online_presence'] if link['platform']=='website'),None)
    if s['status']=='submitted' and (not submitted_site or submitted_site.rstrip('/')!=site.rstrip('/')):
        raise HTTPException(409,'The website must match the submitted inputs; start a new draft to change it')
    if s['status']=='draft':
        draft['brand_name']=draft['brand_name'] or b['name']
        if not submitted_site:draft['online_presence'].insert(0,{'url':site,'platform':'website'})
        elif supplied:
            next(link for link in draft['online_presence'] if link['platform']=='website')['url']=site
    intake={**b['intake'],**draft,'name':draft['brand_name'] or b['name'],'website_url':site,'awaiting_configuration':False}
    connection.execute('UPDATE mi.brand_workspaces SET website_url=%s,intake=%s WHERE id=%s',(site,Jsonb(intake),brand_id))
    source=connection.execute("SELECT id FROM mi.research_sources WHERE brand_id=%s AND provenance='client_input' ORDER BY created_at LIMIT 1",(brand_id,)).fetchone()
    if not source:
        connection.execute("INSERT INTO mi.research_sources(organization_id,brand_id,url,publisher,accessed_at,geographic_scope,provenance) VALUES(%s,%s,%s,'Founder supplied intake',now(),'unknown','client_input')",(b['organization_id'],brand_id,site))
    job=start_crawl(brand_id,CrawlRequest(max_pages=body.max_pages),key,user,connection)
    updated=connection.execute('UPDATE mi.brand_onboarding_sessions SET draft=%s,crawl_job_id=%s,version=version+1,updated_at=now() WHERE id=%s AND brand_id=%s RETURNING *',(Jsonb(draft),job.id,session_id,brand_id)).fetchone()
    from app.services.research_storage import ensure_template
    from app.services.research_prompt import FIELDS
    for task_type in FIELDS:ensure_template(connection,b,user['id'],task_type)
    return {'session':updated,'job':job}


@router.post('/sessions/{session_id}/synthesise',status_code=201)
def build(brand_id:UUID,session_id:UUID,body:VersionAction,refresh:bool=False,user=Depends(current_user),connection=Depends(actor_transaction)):
    b=access(connection,brand_id,True);require_brand(connection,brand_id,'internal:read');lock_key(connection,'brain:'+str(brand_id));s=session(connection,brand_id,session_id)
    if s['version']!=body.expected_version or s['status']!='submitted':raise HTTPException(409,'Submit the current saved inputs first')
    old=connection.execute('SELECT * FROM mi.brand_brain_versions WHERE brand_id=%s AND session_id=%s ORDER BY version DESC LIMIT 1',(brand_id,session_id)).fetchone()
    if old and not refresh:return quality(connection,old)
    job=connection.execute('SELECT mi.brain_job_status(%s,%s) AS job',(brand_id,s['crawl_job_id'])).fetchone()['job']
    if not job or job['status']!='succeeded':raise HTTPException(409,'Real public capture must complete before synthesis; no findings will be fabricated')
    return quality(connection,synthesise(connection,b,s,user['id']))


@router.get('/overview')
def overview(brand_id:UUID,connection=Depends(actor_transaction)):
    b=access(connection,brand_id);latest=version_row(connection,brand_id);result=quality(connection,latest)
    current=connection.execute('SELECT id,version_id,status,created_at,actor_name FROM mi.brand_brain_confirmations WHERE brand_id=%s ORDER BY created_at DESC LIMIT 1',(brand_id,)).fetchone()
    result['confirmed']=current;canonical=latest_profile(connection,brand_id) if bscope(connection,brand_id,'internal:read') else None;result['canonical_profile_ready']=bool(canonical and canonical['status']=='confirmed');result['can_confirm']=connection.execute('SELECT mi.can_org_admin(%s) AS ok',(b['organization_id'],)).fetchone()['ok'];return result


@router.get('/versions')
def versions(brand_id:UUID,connection=Depends(actor_transaction)):
    access(connection,brand_id);return connection.execute('''SELECT v.id,v.version,v.created_at,v.content_sha256,c.status AS confirmation_status
        FROM mi.brand_brain_versions v LEFT JOIN mi.brand_brain_confirmations c ON c.version_id=v.id AND c.brand_id=v.brand_id WHERE v.brand_id=%s ORDER BY v.version DESC LIMIT 100''',(brand_id,)).fetchall()


@router.get('/versions/{version_id}')
def get_version(brand_id:UUID,version_id:UUID,connection=Depends(actor_transaction)):
    access(connection,brand_id);return quality(connection,version_row(connection,brand_id,version_id))


@router.get('/versions/{version_id}/chapters/{chapter}')
def chapter(brand_id:UUID,version_id:UUID,chapter:str,connection=Depends(actor_transaction)):
    access(connection,brand_id);v=project(connection,version_row(connection,brand_id,version_id))
    ch=next((c for c in v['document']['chapters'] if c['key']==chapter),None)
    if not ch:raise HTTPException(404,'Chapter not found')
    return {**ch,'sources':v['document']['sources'],'version_id':version_id}


@router.post('/versions/{version_id}/findings/{finding_id}/review',status_code=201)
def review(brand_id:UUID,version_id:UUID,finding_id:UUID,body:FindingReview,user=Depends(current_user),connection=Depends(actor_transaction)):
    b=access(connection,brand_id,True);v=project(connection,mutable(connection,brand_id,version_id))
    f=next((f for ch in v['document']['chapters'] for f in ch['findings'] if str(f['id'])==str(finding_id)),None)
    if not f:raise HTTPException(404,'Finding not found')
    if f['review_sequence']!=body.expected_sequence:raise HTTPException(409,'Finding changed; reload before reviewing')
    if body.action=='accept' and f['unknown']:raise HTTPException(409,'Unknown information cannot be accepted as a verified finding; edit it or retain the gap')
    if body.action=='accept':require_admin(connection,b['organization_id'])
    if f['provenance']=='founder_confirmed' and body.action in {'edit','mark_unknown'}:require_admin(connection,b['organization_id'])
    connection.execute('''INSERT INTO mi.brand_brain_reviews(organization_id,brand_id,version_id,finding_id,sequence,action,text,reason,actor_name,created_by)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(b['organization_id'],brand_id,version_id,finding_id,body.expected_sequence+1,body.action,body.text,body.reason,user['full_name'],user['id']))
    return quality(connection,version_row(connection,brand_id,version_id))


@router.post('/versions/{version_id}/threads',status_code=201)
def new_thread(brand_id:UUID,version_id:UUID,body:ThreadCreate,user=Depends(current_user),connection=Depends(actor_transaction)):
    b=access(connection,brand_id,True);v=project(connection,mutable(connection,brand_id,version_id))
    ch=next(ch for ch in v['document']['chapters'] if ch['key']==body.chapter)
    if body.finding_id and not any(str(f['id'])==str(body.finding_id) for f in ch['findings']):raise HTTPException(404,'Finding not found in this chapter')
    row=connection.execute('INSERT INTO mi.brand_brain_threads(organization_id,brand_id,version_id,chapter,finding_id,created_by) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *',(b['organization_id'],brand_id,version_id,body.chapter,body.finding_id,user['id'])).fetchone()
    add_message(connection,b,row['id'],body.body,user);return row


def add_message(c,b,thread_id,body,user):
    return c.execute('INSERT INTO mi.brand_brain_comments(organization_id,brand_id,thread_id,body,actor_name,created_by) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *',(b['organization_id'],b['id'],thread_id,body,user['full_name'],user['id'])).fetchone()


def thread_row(c,bid,tid):
    row=c.execute('SELECT * FROM mi.brand_brain_threads WHERE brand_id=%s AND id=%s',(bid,tid)).fetchone()
    if not row:raise HTTPException(404,'Thread not found')
    return row


@router.post('/threads/{thread_id}/comments',status_code=201)
def reply(brand_id:UUID,thread_id:UUID,body:CommentCreate,user=Depends(current_user),connection=Depends(actor_transaction)):
    b=access(connection,brand_id,True);t=thread_row(connection,brand_id,thread_id);mutable(connection,brand_id,t['version_id']);return add_message(connection,b,thread_id,body.body,user)


@router.post('/threads/{thread_id}/state',status_code=201)
def resolve(brand_id:UUID,thread_id:UUID,body:ThreadAction,user=Depends(current_user),connection=Depends(actor_transaction)):
    b=access(connection,brand_id,True);t=thread_row(connection,brand_id,thread_id);mutable(connection,brand_id,t['version_id'])
    events=connection.execute('SELECT id FROM mi.brand_brain_thread_events WHERE brand_id=%s AND thread_id=%s ORDER BY created_at DESC,id DESC LIMIT 1',(brand_id,thread_id)).fetchone()
    if (events['id'] if events else None)!=body.expected_event:raise HTTPException(409,'Thread changed; reload before resolving')
    return connection.execute('INSERT INTO mi.brand_brain_thread_events(organization_id,brand_id,thread_id,action,actor_name,created_by) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *',(b['organization_id'],brand_id,thread_id,body.action,user['full_name'],user['id'])).fetchone()


@router.get('/activity')
def activity(brand_id:UUID,connection=Depends(actor_transaction)):
    access(connection,brand_id)
    # Explicit allowlist: no raw prompt, costs, auth events, or other-module audit payload.
    return connection.execute('SELECT * FROM mi.brain_activity(%s)',(brand_id,)).fetchall()


@router.get('/current')
def confirmed(brand_id:UUID,connection=Depends(actor_transaction)):
    access(connection,brand_id);row=connection.execute('SELECT * FROM mi.brand_brain_confirmations WHERE brand_id=%s ORDER BY created_at DESC LIMIT 1',(brand_id,)).fetchone()
    if not row:raise HTTPException(404,'No confirmed Brand Brain yet')
    return row


@router.post('/versions/{version_id}/confirm',status_code=201)
def confirm(brand_id:UUID,version_id:UUID,body:BrainConfirm,user=Depends(current_user),connection=Depends(actor_transaction)):
    b=access(connection,brand_id,True);require_admin(connection,b['organization_id']);row=mutable(connection,brand_id,version_id);v=quality(connection,row)
    if body.expected_review_sequence!=v['review_sequence']:raise HTTPException(409,'Reviews changed; reload before confirmation')
    selected=set(body.approved_chapters)
    if len(selected)!=len(body.approved_chapters) or not selected.issubset(v['quality']['approved_chapters']):raise HTTPException(409,'Only fully reviewed chapters can be confirmed')
    if not body.partial and selected!=set(CHAPTERS):raise HTTPException(409,'Complete all six chapters or choose partial confirmation')
    if not body.acknowledge_limitations:raise HTTPException(422,'Acknowledge evidence limitations before confirmation')
    if any(not t['resolved'] and t['chapter'] in selected for t in v['threads']):raise HTTPException(409,'Resolve comments in the selected chapters first')
    snapshot=deepcopy(v['document']);snapshot['chapters']=[ch for ch in snapshot['chapters'] if ch['key'] in selected]
    snapshot['unconfirmed_chapters']=[key for key in CHAPTERS if key not in selected]
    return connection.execute('''INSERT INTO mi.brand_brain_confirmations(organization_id,brand_id,version_id,review_sequence,approved_chapters,status,snapshot,content_sha256,actor_name,created_by)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',(b['organization_id'],brand_id,version_id,v['review_sequence'],body.approved_chapters,'partial' if selected!=set(CHAPTERS) else 'confirmed',Jsonb(snapshot),payload_hash(snapshot),user['full_name'],user['id'])).fetchone()


@router.post('/versions/{version_id}/fork',status_code=201)
def fork(brand_id:UUID,version_id:UUID,user=Depends(current_user),connection=Depends(actor_transaction)):
    b=access(connection,brand_id,True);require_brand(connection,brand_id,'internal:read');lock_key(connection,'brain:'+str(brand_id));old=version_row(connection,brand_id,version_id);projected=project(connection,old)
    # Restore into a NEW draft. The prior confirmed version and all originals remain immutable.
    body=deepcopy(old['body'])
    for ch in body['chapters']:
        for f in ch['findings']:
            current=next(f2 for c2 in projected['document']['chapters'] for f2 in c2['findings'] if f2['id']==f['id'])
            f.update(text=current['text'],provenance=current['provenance'],unknown=current['unknown'],confidence=current['confidence'])
    n=version_row(connection,brand_id)['version']+1
    row=connection.execute('''INSERT INTO mi.brand_brain_versions(organization_id,brand_id,version,session_id,profile_artifact_id,dossier_id,body,content_sha256,created_by)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',(b['organization_id'],brand_id,n,old['session_id'],old['profile_artifact_id'],old['dossier_id'],Jsonb(body),payload_hash(body),user['id'])).fetchone()
    return quality(connection,row)

from app.research_models import ResearchStart

@router.post('/sessions/{session_id}/research',status_code=202)
def start_existing_research(brand_id:UUID,session_id:UUID,body:ResearchStart,idempotency_key:str=Header(...),user=Depends(current_user),connection=Depends(actor_transaction)):
    access(connection,brand_id,True);s=session(connection,brand_id,session_id)
    from app.research_api import start
    result=start(brand_id,body,idempotency_key,user,connection)
    connection.execute('UPDATE mi.brand_onboarding_sessions SET research_job_id=%s,version=version+1,updated_at=now() WHERE id=%s AND brand_id=%s',(result.job_id,session_id,brand_id))
    return result

from app.models import OnboardingInput,OnboardingSave
from app.api_types import ProfileConfirm

@router.post('/versions/{version_id}/canonical-profile')
def promote_canonical(brand_id:UUID,version_id:UUID,body:OnboardingInput,user=Depends(current_user),connection=Depends(actor_transaction)):
    """Reuse the existing intake confirmation contract, including its critical-gap gates."""
    b=access(connection,brand_id,True);require_admin(connection,b['organization_id']);lock_key(connection,'brain:'+str(brand_id))
    record=connection.execute("SELECT * FROM mi.brand_brain_confirmations WHERE brand_id=%s AND version_id=%s AND status='confirmed'",(brand_id,version_id)).fetchone()
    if not record:raise HTTPException(409,'All six chapters must be confirmed before canonical-profile promotion')
    current=connection.execute('SELECT version_id FROM mi.brand_brain_confirmations WHERE brand_id=%s ORDER BY created_at DESC LIMIT 1',(brand_id,)).fetchone()
    if current['version_id']!=version_id:raise HTTPException(409,'A newer confirmed Brand Brain exists')
    workflow=workflow_for_brand(connection,brand_id)
    if workflow['state'] not in {'awaiting_onboarding','awaiting_profile_confirmation'}:raise HTTPException(409,'The existing canonical-profile workflow is not accepting intake; its current confirmed profile is preserved')
    from app.main import save_onboarding,submit_onboarding,confirm_profile,own_onboarding
    from app.models import VersionRequest
    values=body.model_dump(mode='json');chapters=record['snapshot']['chapters']
    mapped={f['field']:f['text'] for ch in chapters for f in ch['findings'] if f['review_status']=='approved' and not f['unknown']}
    for key in ['business_description','business_model','audience_description']:
        if not values.get(key) and mapped.get(key):values[key]=mapped[key]
    if not values.get('primary_objective'):
        values['primary_objective']='; '.join(f['text'] for ch in chapters if ch['key']=='objectives' for f in ch['findings'] if not f['unknown'])
    values['brand_name']=record['snapshot']['brand_name']
    if not values.get('target_geographies'):values['target_geographies']=['India']
    old=own_onboarding(connection,brand_id,user['id'])
    saved=save_onboarding(brand_id,OnboardingSave(expected_version=old['version'] if old else 0,answers=OnboardingInput.model_validate(values)),user,connection)
    submitted=submit_onboarding(brand_id,VersionRequest(expected_version=saved.version),user,connection)
    profile=latest_profile(connection,brand_id)
    return confirm_profile(brand_id,ProfileConfirm(expected_profile_version=profile['version'],onboarding_response_id=submitted.id),user,connection)

@router.post('/sessions/{session_id}/prepare-research')
def prepare_existing_intake(brand_id:UUID,session_id:UUID,body:OnboardingInput,user=Depends(current_user),connection=Depends(actor_transaction)):
    """Targeted follow-up only when the existing paid research gate needs it."""
    b=access(connection,brand_id,True);require_admin(connection,b['organization_id']);s=session(connection,brand_id,session_id)
    if s['status']!='submitted':raise HTTPException(409,'Submit the four setup inputs first')
    lock_key(connection,'profile:'+str(brand_id));w=workflow_for_brand(connection,brand_id,lock=True)
    if w['state'] not in {'awaiting_onboarding','awaiting_profile_confirmation'}:raise HTTPException(409,'This profile is not awaiting intake confirmation; existing confirmed information is preserved')
    from app.main import save_onboarding,submit_onboarding,confirm_profile,own_onboarding
    from app.models import VersionRequest
    data=body.model_dump(mode='json')
    data['brand_name']=s['draft']['brand_name']
    if not data.get('primary_objective'):data['primary_objective']='; '.join(s['draft']['objectives'])
    previous=own_onboarding(connection,brand_id,user['id'])
    saved=save_onboarding(brand_id,OnboardingSave(expected_version=previous['version'] if previous else 0,answers=OnboardingInput.model_validate(data)),user,connection)
    submitted=submit_onboarding(brand_id,VersionRequest(expected_version=saved.version),user,connection)
    latest=latest_profile(connection,brand_id)
    return confirm_profile(brand_id,ProfileConfirm(expected_profile_version=latest['version'],onboarding_response_id=submitted.id),user,connection)
