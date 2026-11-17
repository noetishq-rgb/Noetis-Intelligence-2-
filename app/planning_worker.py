"""Phase 4 worker: leased, budgeted proposals with no automatic human decisions."""
import argparse
import json
import time
from uuid import UUID
from app.config import settings
from app.planning_models import STAGES,SCHEMAS,PlanningValidation
from datetime import datetime,timezone
from app.repository import workflow_for_brand,transition
from app.worker import transaction,locked_job,save_traces
from app.research_worker import claim,pause,gate,persist_run,model_trace
from app.services import planning as service
from app.services.planning import PlanningError,need
from app.services.anthropic_provider import get_planning_provider,ProviderError
from app.services.research_budget import reserve,settle,BudgetError


def prepare(actor,job):
    with transaction(actor) as c:
        workflow_for_brand(c,job['brand_id'],lock=True)
        if not locked_job(c,job):return None,None
        ctx=service.context(c,job['brand_id'],job['kind'])
        need(ctx['parents']==job['payload']['parents'],'planning_inputs_changed')
        if job['kind']=='thirty_day_plan':need({UUID(u) for u in job['payload']['owner_user_ids']}<=ctx['allowed_owners'],'planning_owners_changed')
        template=c.execute('SELECT * FROM mi.prompt_templates WHERE id=%s AND brand_id=%s',(job['payload']['template_id'],job['brand_id'])).fetchone()
        need(template and template['output_schema']==SCHEMAS[job['kind']],'planning_template_unavailable')
        return service.compile_prompt(ctx,job['kind'],job['payload'],template),ctx


def finish(actor,job,dispatch,request,result,ctx,traces,latency):
    output,validation=service.evaluate(job['kind'],result.output_json,ctx,job['payload'])
    error=None if output else 'planning_output_validation_failed'
    with transaction(actor) as c:
        w=workflow_for_brand(c,job['brand_id'],lock=True)
        settled=settle(c,dispatch,result,latency_ms=latency,error=error)
        save_traces(c,job,actor,traces)
        role=STAGES[job['kind']][4]
        if not locked_job(c,job):
            live=c.execute('SELECT status FROM mi.jobs WHERE id=%s AND brand_id=%s',(job['id'],job['brand_id'])).fetchone()
            discard='discarded_after_cancellation' if live and live['status']=='cancelled' else 'discarded_after_lease_loss'
            persist_run(c,job,settled,request,result,ctx['profile'],None,False,discard,latency,w,validation,role)
            return
        try:
            current=service.context(c,job['brand_id'],job['kind'])
            need(current['parents']==job['payload']['parents'],'planning_inputs_changed')
            # Membership and selected KPI permissions are checked again after network work.
            if output:output=service.validate_output(job['kind'],output.model_dump(mode='json'),current,job['payload'])
        except PlanningError as exc:
            validation=PlanningValidation(checked_at=datetime.now(timezone.utc),valid=False,issue_codes=[exc.code])
            persist_run(c,job,settled,request,result,ctx['profile'],None,False,exc.code,latency,w,validation,role)
            transition(c,w,'run_failed',gate(w),last_error=exc.code)
            c.execute("UPDATE mi.jobs SET status='failed',last_error=%s,lease_id=NULL,lease_expires_at=NULL WHERE id=%s AND brand_id=%s",(exc.code,job['id'],job['brand_id']))
            return
        w=workflow_for_brand(c,job['brand_id']);aid=None
        if w['budget_breached']:
            persist_run(c,job,settled,request,result,ctx['profile'],None,False,'provider_usage_exceeded_reservation',latency,w,validation,role)
            transition(c,w,'budget_exhausted',gate(w),last_error='provider_usage_exceeded_reservation');status='failed';error='provider_usage_exceeded_reservation'
        else:
            if output:_,aid=service.persist_output(c,job,settled,current,output,validation,actor)
            persist_run(c,job,settled,request,result,ctx['profile'],aid,output is not None,error,latency,w,validation,role)
            if output:
                transition(c,w,STAGES[job['kind']][3],gate(w),last_error=None);status='succeeded'
            else:
                updated=transition(c,w,'validation_failed',gate(w,validation_failures=w['validation_failures']),last_error=error)
                status='retry' if updated['state']==STAGES[job['kind']][1] and job['attempts']<job['max_attempts'] else 'failed'
                if status=='failed' and updated['state']==STAGES[job['kind']][1]:transition(c,updated,'run_failed',gate(updated),last_error=error)
        c.execute("UPDATE mi.jobs SET status=%s,last_error=%s,available_at=now()+interval '2 seconds',lease_id=NULL,lease_expires_at=NULL WHERE id=%s AND brand_id=%s",(status,error,job['id'],job['brand_id']))


def run_once(actor,*,provider=None,stage=None):
    job=None
    for name in ([stage] if stage else STAGES):
        queued,running,start,_,_=STAGES[name]
        job=claim(actor,kind=name,queue_state=queued,running_state=running,start_event=start,scope='planning:run')
        if job:break
    if not job:return False
    traces=[];dispatch=None
    try:
        request,ctx=prepare(actor,job)
        if request is None:return True
        provider=provider or get_planning_provider()
        started=time.monotonic()
        try:count=provider.count_tokens(request)
        except ProviderError as exc:
            traces.append(model_trace(provider,request,'count',started,exc.code));raise
        traces.append(model_trace(provider,request,'count',started))
        with transaction(actor) as c:
            w=workflow_for_brand(c,job['brand_id'],lock=True)
            if not locked_job(c,job):save_traces(c,job,actor,traces);return True
            dispatch=reserve(c,job,w,ctx['profile'],request,count,getattr(provider,'name','configured'),getattr(provider,'model','configured'),actor)
            save_traces(c,job,actor,traces);traces=[]
        with transaction(actor) as c:
            workflow_for_brand(c,job['brand_id'],lock=True)
            if not locked_job(c,job):settle(c,dispatch,error='cancelled_before_dispatch');return True
            c.execute("UPDATE mi.model_dispatches SET state='dispatched',dispatched_at=now() WHERE id=%s AND brand_id=%s",(dispatch['id'],job['brand_id']))
        started=time.monotonic()
        try:result=provider.generate(request)
        except ProviderError as exc:
            traces.append(model_trace(provider,request,'generate',started,exc.code));raise
        traces.append(model_trace(provider,request,'generate',started))
        finish(actor,job,dispatch,request,result,ctx,traces,int((time.monotonic()-started)*1000))
    except PlanningError as exc:pause(actor,job,exc.code,traces)
    except ProviderError as exc:pause(actor,job,exc.code,traces,dispatch=dispatch,uncertain=exc.uncertain,retryable=exc.retryable)
    except BudgetError as exc:pause(actor,job,str(exc),traces,budget=True)
    return True


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');parser.add_argument('--stage',choices=list(STAGES));args=parser.parse_args()
    if not settings().service_user_id:raise SystemExit('MI_SERVICE_USER_ID is required')
    actor=UUID(settings().service_user_id)
    while True:
        worked=run_once(actor,stage=args.stage)
        if args.once:break
        if not worked:time.sleep(2)

if __name__=='__main__':main()
