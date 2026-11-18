"""Scoped research worker. Evidence is captured before a single admitted model call."""
import argparse
import json
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4
from psycopg.types.json import Jsonb
from app.config import settings
from app.models import PromptRun, TokenUsage, Money
from app.research_models import SourceSeed
from app.repository import transition, workflow_for_brand, payload_hash
from app.services.anthropic_provider import get_model_provider, ProviderError
from app.services.research_budget import reserve, settle, BudgetError
from app.services.research_prompt import compile_prompt
from app.services.research_sources import collect
from app.services.research_storage import evidence_rows, persist_evidence, persist_dossier
from app.services.research_validation import evaluate_output
from app.worker import transaction, validate_service, locked_job, save_traces, _bound_database_url
from app.workflow import GateContext


from app.models import BrandProfileV11 as BrandProfile

def gate(workflow, **kwargs):
    return GateContext(calls=workflow['agent_calls'], max_calls=workflow['max_agent_calls'],
        tokens=workflow['tokens_used']+workflow['tokens_reserved'], token_budget=workflow['token_budget'], **kwargs)


def load_profile(connection, job):
    row = connection.execute('SELECT * FROM mi.brand_profiles WHERE brand_id=%s AND artifact_id=%s',
        (job['brand_id'], job['payload']['profile_artifact_id'])).fetchone()
    if not row or row['status'] != 'confirmed':
        raise ValueError('confirmed_profile_unavailable')
    return row


def claim(actor, *, kind='research', queue_state='research_queued', running_state='researching', start_event='start_research', scope='research:run'):
    with transaction(actor) as connection:
        validate_service(connection, actor)
        candidates = connection.execute("""SELECT id,workflow_id,brand_id FROM mi.jobs WHERE kind=%s
            AND mi.has_brand_scope(brand_id,%s) AND ((status IN('queued','retry') AND available_at<=now())
            OR (status='running' AND lease_expires_at<now())) ORDER BY available_at,id LIMIT 16""",(kind,scope)).fetchall()
        for candidate in candidates:
            workflow = connection.execute('SELECT * FROM mi.workflow_instances WHERE id=%s AND brand_id=%s FOR UPDATE SKIP LOCKED',
                (candidate['workflow_id'], candidate['brand_id'])).fetchone()
            if not workflow:
                continue
            job = connection.execute('SELECT * FROM mi.jobs WHERE id=%s AND brand_id=%s FOR UPDATE SKIP LOCKED',
                (candidate['id'], candidate['brand_id'])).fetchone()
            if not job or job['status'] not in {'queued','retry','running'}:
                continue
            if job['status']=='running' and job['lease_expires_at']>=datetime.now(timezone.utc):
                continue
            if workflow['state'] not in {queue_state,running_state}:
                connection.execute("UPDATE mi.jobs SET status='cancelled' WHERE id=%s AND brand_id=%s", (job['id'],job['brand_id']))
                continue
            pending = connection.execute("SELECT * FROM mi.model_dispatches WHERE job_id=%s AND brand_id=%s AND state IN('reserved','dispatched') FOR UPDATE", (job['id'],job['brand_id'])).fetchone()
            if pending:
                if pending['state']=='dispatched':
                    settle(connection,pending,uncertain=True,error='worker_lost_after_dispatch')
                    transition(connection,workflow,'provider_outcome_unknown',gate(workflow),last_error='worker_lost_after_dispatch')
                    connection.execute("UPDATE mi.jobs SET status='failed',last_error='worker_lost_after_dispatch',lease_id=NULL,lease_expires_at=NULL WHERE id=%s AND brand_id=%s", (job['id'],job['brand_id']))
                    continue
                settle(connection,pending,error='lease_expired_before_dispatch')
            if job['attempts']>=job['max_attempts']:
                transition(connection,workflow,'run_failed',gate(workflow),last_error='research_attempts_exhausted')
                connection.execute("UPDATE mi.jobs SET status='failed',last_error='research_attempts_exhausted' WHERE id=%s AND brand_id=%s", (job['id'],job['brand_id']))
                continue
            job = connection.execute("UPDATE mi.jobs SET status='running',attempts=attempts+1,lease_id=%s,lease_expires_at=now()+interval '900 seconds' WHERE id=%s AND brand_id=%s RETURNING *", (uuid4(),job['id'],job['brand_id'])).fetchone()
            if workflow['state']==queue_state:
                transition(connection,workflow,start_event,gate(workflow,confirmed_profile=True))
            return job
    return None


def pause(actor,job,code,traces,*,dispatch=None,uncertain=False,retryable=False,budget=False):
    with transaction(actor) as connection:
        workflow = workflow_for_brand(connection,job['brand_id'],lock=True)
        if dispatch:
            settle(connection,dispatch,uncertain=uncertain,error=code)
        save_traces(connection,job,actor,traces)
        row = locked_job(connection,job)
        if not row:
            return
        if uncertain:
            event = 'provider_outcome_unknown'
        elif budget:
            event = 'budget_exhausted'
        elif code=='no_accessible_research_evidence':
            event = 'request_inputs'
        elif retryable and row['attempts']<row['max_attempts']:
            connection.execute("UPDATE mi.jobs SET status='retry',last_error=%s,available_at=now()+interval '2 seconds',lease_id=NULL,lease_expires_at=NULL WHERE id=%s AND brand_id=%s", (code,job['id'],job['brand_id']))
            return
        else:
            event = 'run_failed'
        transition(connection,workflow,event,gate(workflow),last_error=code)
        connection.execute("UPDATE mi.jobs SET status='failed',last_error=%s,lease_id=NULL,lease_expires_at=NULL WHERE id=%s AND brand_id=%s", (code,job['id'],job['brand_id']))


def capture(actor,job,collector):
    if job['collection_complete']:
        return True
    traces=[]
    seeds=[SourceSeed.model_validate(value) for value in job['payload']['sources']]
    sources,limitations=collector(seeds,job['payload']['search_queries'],trace=traces.append)
    with transaction(actor) as connection:
        workflow_for_brand(connection,job['brand_id'],lock=True)
        if not locked_job(connection,job):
            save_traces(connection,job,actor,traces)
            return False
        for source in sources:
            persist_evidence(connection,job,source)
        save_traces(connection,job,actor,traces)
        connection.execute('UPDATE mi.jobs SET collection_complete=true,source_limitations=%s WHERE id=%s AND brand_id=%s', (Jsonb(limitations),job['id'],job['brand_id']))
    if not sources:
        pause(actor,job,'no_accessible_research_evidence',[])
        return False
    return True


def prepare(actor,job):
    with transaction(actor) as connection:
        workflow_for_brand(connection,job['brand_id'],lock=True)
        if not locked_job(connection,job):
            return None,None,None
        profile=load_profile(connection,job)
        evidence=evidence_rows(connection,job['brand_id'],job_id=job['id'])
        template=connection.execute("SELECT * FROM mi.prompt_templates WHERE id=%s AND brand_id=%s", (job['payload']['template_id'], job['brand_id'])).fetchone()
        if not template or template['output_schema']!='research-dossier-v1.1':
            raise ValueError('research_template_unavailable')
        request=compile_prompt(profile,evidence,job['payload']['areas'],job['payload']['task_type'],template=template, brief=job["payload"].get("brief"))
    return request,profile,evidence


def model_trace(provider,request,method,start,error=None):
    return {'endpoint': 'https://api.anthropic.com/v1/messages'+('/count_tokens' if method=='count' else ''),
        'method':'POST','response_status':None if error else 200,'error_code':error,
        'latency_ms':int((time.monotonic()-start)*1000),'request_sha256':payload_hash(request.model_dump(mode='json'))}


def persist_run(connection,job,dispatch,request,result,profile,artifact_id,valid,error,latency,workflow,validation=None,role='research_analyst'):
    run_id=dispatch['id']
    prompt=PromptRun(run_id=run_id,brand_id=job['brand_id'],workflow_id=job['workflow_id'],task_type=request.task_type,
        template_id=UUID(job['payload']['template_id']),template_version=request.template_version,provider=result.provider,model=result.model,
        input_references=request.context,compiled_prompt_sha256=dispatch['request_sha256'],output_schema=request.output_schema_name,
        output_artifact_id=artifact_id,usage=TokenUsage(input_tokens=result.input_tokens,output_tokens=result.output_tokens,token_budget=workflow['token_budget']),
        cost=Money(amount_paise=dispatch['cost_paise']) if dispatch['cost_paise'] is not None else None,
        latency_ms=latency,attempt=min(2,workflow['validation_failures']+1),status='valid' if valid else 'invalid' if error in {'research_output_validation_failed','planning_output_validation_failed'} else 'failed',
        error_code=error,started_at=dispatch['created_at'],finished_at=datetime.now(timezone.utc))
    connection.execute('INSERT INTO mi.prompt_runs(id,organization_id,brand_id,workflow_id,template_id,body,input_tokens,output_tokens,cost_paise) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (run_id,job['organization_id'],job['brand_id'],job['workflow_id'],job['payload']['template_id'],Jsonb(prompt.model_dump(mode='json')),result.input_tokens,result.output_tokens,dispatch['cost_paise']))
    agent_id=uuid4()
    connection.execute("INSERT INTO mi.agent_runs(id,organization_id,brand_id,workflow_id,prompt_run_id,agent_role,status,attempt,validation_failures,latency_ms,error_code) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (agent_id,job['organization_id'],job['brand_id'],job['workflow_id'],run_id,role,'succeeded' if valid else 'failed',job['attempts'],min(2,workflow['validation_failures']+int(error in {'research_output_validation_failed','planning_output_validation_failed'})),latency,error))
    try:
        output={'output':json.loads(result.output_json)}
    except (ValueError,TypeError):
        output={'raw_text':result.output_json[:200000]}
    if validation:
        output['validation']=validation.model_dump(mode='json')
    connection.execute('INSERT INTO mi.agent_outputs(organization_id,brand_id,agent_run_id,artifact_id,output_schema,valid,body) VALUES(%s,%s,%s,%s,%s,%s,%s)',
        (job['organization_id'],job['brand_id'],agent_id,artifact_id,request.output_schema_name,valid,Jsonb(output)))


def finish(actor,job,dispatch,request,result,profile,evidence,traces,latency):
    dossier,validation=evaluate_output(result.output_json,job['brand_id'],profile['version'],evidence,json.loads(request.context_json)['research_as_of'])
    error=None if dossier else 'research_output_validation_failed'
    with transaction(actor) as connection:
        workflow=workflow_for_brand(connection,job['brand_id'],lock=True)
        settled=settle(connection,dispatch,result,latency_ms=latency,error=error)
        save_traces(connection,job,actor,traces)
        if not locked_job(connection,job):
            # Charges and the raw response survive cancellation, generated artifacts do not.
            live=connection.execute('SELECT status FROM mi.jobs WHERE id=%s AND brand_id=%s',(job['id'],job['brand_id'])).fetchone()
            discard='discarded_after_cancellation' if live and live['status']=='cancelled' else 'discarded_after_lease_loss'
            persist_run(connection,job,settled,request,result,profile,None,False,discard,latency,workflow,validation)
            return
        workflow=workflow_for_brand(connection,job['brand_id'])
        if workflow['approved_profile_id']!=profile['artifact_id']:
            pause_code='research_profile_changed'
            persist_run(connection,job,settled,request,result,profile,None,False,pause_code,latency,workflow,validation)
            transition(connection,workflow,'run_failed',gate(workflow),last_error=pause_code)
            connection.execute("UPDATE mi.jobs SET status='failed',last_error=%s,lease_id=NULL,lease_expires_at=NULL WHERE id=%s AND brand_id=%s", (pause_code,job['id'],job['brand_id']))
            return
        artifact_id=None
        if dossier:
            artifact_id,_=persist_dossier(connection,job,profile,dossier,actor)
        persist_run(connection,job,settled,request,result,profile,artifact_id,dossier is not None,error,latency,workflow,validation)
        if workflow['budget_breached']:
            transition(connection,workflow,'budget_exhausted',gate(workflow),last_error='provider_usage_exceeded_reservation')
            status,code='failed','provider_usage_exceeded_reservation'
        elif dossier:
            transition(connection,workflow,'research_succeeded',gate(workflow),last_error=None)
            status,code='succeeded',None
        else:
            updated=transition(connection,workflow,'validation_failed',gate(workflow,validation_failures=workflow['validation_failures']),last_error=error)
            status='retry' if updated['state']=='researching' and job['attempts']<job['max_attempts'] else 'failed'
            if status=='failed' and updated['state']=='researching':
                transition(connection,updated,'run_failed',gate(updated),last_error=error)
            code=error
        connection.execute("UPDATE mi.jobs SET status=%s,last_error=%s,available_at=now()+interval '2 seconds',lease_id=NULL,lease_expires_at=NULL WHERE id=%s AND brand_id=%s", (status,code,job['id'],job['brand_id']))


def run_once(actor: UUID, *, provider=None, collector=collect, database_url=None):
    token = _bound_database_url.set(database_url)
    try:
        return _run_once(actor, provider=provider, collector=collector)
    finally:
        _bound_database_url.reset(token)


def _run_once(actor: UUID, *, provider=None, collector=collect):
    job=claim(actor)
    if not job:
        return False
    if not capture(actor,job,collector):
        return True
    request,profile,evidence=prepare(actor,job)
    if request is None:
        return True
    traces=[]
    dispatch=None
    try:
        provider=provider or get_model_provider()
        started=time.monotonic()
        try:
            count=provider.count_tokens(request)
            traces.append(model_trace(provider,request,'count',started))
        except ProviderError as exc:
            traces.append(model_trace(provider,request,'count',started,exc.code))
            raise
        with transaction(actor) as connection:
            workflow=workflow_for_brand(connection,job['brand_id'],lock=True)
            if not locked_job(connection,job):
                save_traces(connection,job,actor,traces)
                return True
            dispatch=reserve(connection,job,workflow,profile,request,count,getattr(provider,'name','custom'),getattr(provider,'model','configured'),actor)
            save_traces(connection,job,actor,traces)
            traces=[]
        with transaction(actor) as connection:
            workflow_for_brand(connection,job['brand_id'],lock=True)
            if not locked_job(connection,job):
                settle(connection,dispatch,error='cancelled_before_dispatch')
                return True
            connection.execute("UPDATE mi.model_dispatches SET state='dispatched',dispatched_at=now() WHERE id=%s AND brand_id=%s", (dispatch['id'],job['brand_id']))
        started=time.monotonic()
        try:
            result=provider.generate(request)
            traces.append(model_trace(provider,request,'generate',started))
        except ProviderError as exc:
            traces.append(model_trace(provider,request,'generate',started,exc.code))
            raise
        finish(actor,job,dispatch,request,result,profile,evidence,traces,int((time.monotonic()-started)*1000))
    except ProviderError as exc:
        pause(actor,job,exc.code,traces,dispatch=dispatch,uncertain=exc.uncertain,retryable=exc.retryable)
    except BudgetError as exc:
        pause(actor,job,str(exc),traces,budget=True)
    return True


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    if not settings().service_user_id:
        raise SystemExit('MI_SERVICE_USER_ID is required')
    actor=UUID(settings().service_user_id)
    while True:
        worked=run_once(actor)
        if args.once:
            break
        if not worked:
            time.sleep(2)


if __name__=='__main__':
    main()
