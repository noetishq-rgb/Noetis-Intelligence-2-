"""Authenticated Phase 4 proposal and founder review contracts."""
import json
from uuid import UUID,uuid4
from fastapi import APIRouter,Depends,Header,HTTPException,Query
from psycopg.types.json import Jsonb
from app.auth import actor_transaction,current_user,require_brand,require_admin
from app.repository import workflow_for_brand,transition,lock_key,payload_hash
from app.workflow import GateContext
from app.planning_models import (PlanningStart,PlanningReview,PlanningAccepted,PlanningRunView,PlanningOutputView,
    PlanningReviewView,PlanningValidation,ArtifactRef,MODELS,STAGES,Stage)
from app.services import planning as service
from app.services.planning import PlanningError,need
from app.services.anthropic_provider import get_planning_provider,ProviderError
from app.api_types import StatusView

router=APIRouter(prefix='/v1/brands/{brand_id}/planning',tags=['Phase 4 planning'])
EVENTS={'strategy':'queue_strategy','strategy_critique':'queue_red_team','thirty_day_plan':'queue_plan','creative_brief':'queue_creative'}
REVIEW_EVENTS={'strategy':('approve_strategy','request_strategy_revision'),'thirty_day_plan':('approve_plan','request_plan_revision'),'creative_brief':('approve_creative','request_creative_revision')}
REVIEW_STATES={'strategy':'awaiting_strategy_selection','thirty_day_plan':'awaiting_plan_approval','creative_brief':'awaiting_creative_approval'}


def gates(w,**kw):
    return GateContext(calls=w['agent_calls'],max_calls=w['max_agent_calls'],tokens=w['tokens_used']+w['tokens_reserved'],token_budget=w['token_budget'],**kw)


def checked_context(connection,brand_id,stage):
    try:return service.context(connection,brand_id,stage)
    except PlanningError as exc:raise HTTPException(409,{'code':exc.code})


@router.post('/templates/initialize',response_model=StatusView)
def templates(brand_id:UUID,user=Depends(current_user),connection=Depends(actor_transaction)):
    brand=require_brand(connection,brand_id,'internal:read');require_admin(connection,brand['organization_id'])
    lock_key(connection,'planning-templates:'+str(brand_id))
    for stage in STAGES:service.ensure_template(connection,brand,user['id'],stage)
    return StatusView(status='planning_templates_ready')


@router.get('/owners',response_model=list[UUID])
def list_owners(brand_id:UUID,connection=Depends(actor_transaction)):
    require_brand(connection,brand_id,'internal:read')
    return sorted(service.owners(connection,brand_id),key=str)


@router.post('/runs',response_model=PlanningAccepted,status_code=202)
def start(brand_id:UUID,body:PlanningStart,idempotency_key:str=Header(...),user=Depends(current_user),connection=Depends(actor_transaction)):
    brand=require_brand(connection,brand_id,'planning:run')
    if not 8<=len(idempotency_key)<=128 or not idempotency_key.isascii() or not idempotency_key.isprintable():raise HTTPException(422,'Use an 8 to 128 character Idempotency-Key')
    lock_key(connection,f'planning:{brand_id}:{user["id"]}:{idempotency_key}')
    hashed=payload_hash(body.model_dump(mode='json'))
    old=connection.execute("SELECT * FROM mi.idempotency_keys WHERE brand_id=%s AND actor_id=%s AND operation='planning' AND key=%s",(brand_id,user['id'],idempotency_key)).fetchone()
    if old:
        if old['request_hash']!=hashed:raise HTTPException(409,'Idempotency key used with different inputs')
        return PlanningAccepted.model_validate(old['response'])
    w=workflow_for_brand(connection,brand_id,lock=True)
    if w['version']!=body.expected_workflow_version:raise HTTPException(409,'Workflow changed; reload first')
    ctx=checked_context(connection,brand_id,body.stage)
    queued,running,_,_,_=STAGES[body.stage]
    if w['state'] in {'failed','needs_human_review','awaiting_inputs'} and w['resume_state']==queued:
        require_admin(connection,brand['organization_id'])
        unknown=connection.execute("SELECT id FROM mi.model_dispatches WHERE brand_id=%s AND workflow_id=%s AND state='outcome_unknown' LIMIT 1",(brand_id,w['id'])).fetchone()
        if unknown and not body.acknowledge_uncertain_charge:raise HTTPException(409,'Explicit acknowledgement of uncertain prior charge is required')
        event='inputs_provided' if w['state']=='awaiting_inputs' else 'resume_after_review'
    elif w['state']==queued or body.stage=='strategy' and w['state']=='ready_for_strategy':event=EVENTS[body.stage]
    else:raise HTTPException(409,'This generation stage is unavailable in the current state')
    if body.stage=='thirty_day_plan':
        if body.starts_on is None:raise HTTPException(422,'A plan start date is required')
        chosen=body.owner_user_ids or [user['id']]
        if len(chosen)!=len(set(chosen)) or not set(chosen)<=ctx['allowed_owners']:raise HTTPException(409,'Owners must be active assigned agency humans in this brand')
    else:
        if body.starts_on is not None or body.owner_user_ids:raise HTTPException(422,'Dates and owners apply only to the plan stage')
        chosen=[]
    try:get_planning_provider()
    except ProviderError as exc:raise HTTPException(503,exc.code)
    lock_key(connection,'planning-templates:'+str(brand_id))
    template=service.ensure_template(connection,brand,user['id'],body.stage)
    updated=transition(connection,w,event,gates(w,human=True,confirmed_profile=True,validated_dossier=True,resume_state=queued),body.expected_workflow_version,last_error=None)
    payload=body.model_dump(mode='json');payload.update(owner_user_ids=[str(u) for u in chosen],profile_artifact_id=str(ctx['profile']['artifact_id']),research_artifact_id=str(ctx['research']['artifact_id']),template_id=str(template['id']),parents=ctx['parents'])
    prior=service.latest_output(connection,brand_id,body.stage)
    if prior:
        feedback=connection.execute('SELECT details FROM mi.planning_reviews WHERE output_id=%s AND brand_id=%s',(prior['id'],brand_id)).fetchone()
        if feedback and feedback['details']['decision']!='approved':payload['revision_feedback']=feedback['details']['reason']
    job=connection.execute('INSERT INTO mi.jobs(organization_id,brand_id,workflow_id,kind,payload,created_by) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id',(brand['organization_id'],brand_id,w['id'],body.stage,Jsonb(payload),user['id'])).fetchone()
    result=PlanningAccepted(brand_id=brand_id,job_id=job['id'],workflow_id=w['id'],stage=body.stage,state=updated['state'])
    connection.execute("INSERT INTO mi.idempotency_keys(organization_id,brand_id,actor_id,operation,key,request_hash,response) VALUES(%s,%s,%s,'planning',%s,%s,%s)",(brand['organization_id'],brand_id,user['id'],idempotency_key,hashed,Jsonb(result.model_dump(mode='json'))))
    return result


@router.get('/runs/{job_id}',response_model=PlanningRunView)
def run_view(brand_id:UUID,job_id:UUID,connection=Depends(actor_transaction)):
    require_brand(connection,brand_id,'internal:read')
    job=connection.execute("SELECT * FROM mi.jobs WHERE id=%s AND brand_id=%s AND kind IN('strategy','strategy_critique','thirty_day_plan','creative_brief')",(job_id,brand_id)).fetchone()
    if not job:raise HTTPException(404,'Planning run not found')
    w=workflow_for_brand(connection,brand_id)
    output=connection.execute('SELECT id FROM mi.planning_outputs WHERE job_id=%s AND brand_id=%s',(job_id,brand_id)).fetchone()
    raw=connection.execute("""SELECT o.body->'validation' AS validation FROM mi.model_dispatches d
        JOIN mi.agent_runs r ON r.prompt_run_id=d.id AND r.brand_id=d.brand_id JOIN mi.agent_outputs o ON o.agent_run_id=r.id AND o.brand_id=r.brand_id
        WHERE d.job_id=%s AND d.brand_id=%s ORDER BY d.created_at DESC LIMIT 1""",(job_id,brand_id)).fetchone()
    return PlanningRunView(job_id=job_id,stage=job['kind'],status=job['status'],attempts=job['attempts'],last_error=job['last_error'],output_id=output['id'] if output else None,
        latest_validation=PlanningValidation.model_validate(raw['validation']) if raw and raw['validation'] else None,
        model_calls=w['agent_calls'],tokens_used=w['tokens_used'],tokens_reserved=w['tokens_reserved'],cost_used_paise=w['cost_used_paise'],cost_reserved_paise=w['cost_reserved_paise'],pricing_complete=w['pricing_complete'])


def output_view(connection,brand_id,oid):
    row=connection.execute('SELECT o.*,a.version,a.kind,a.content_sha256 FROM mi.planning_outputs o JOIN mi.artifacts a ON a.id=o.artifact_id AND a.brand_id=o.brand_id WHERE o.id=%s AND o.brand_id=%s',(oid,brand_id)).fetchone()
    if not row:raise HTTPException(404,'Planning artifact not found')
    review=connection.execute('SELECT * FROM mi.planning_reviews WHERE output_id=%s AND brand_id=%s',(oid,brand_id)).fetchone()
    view=PlanningOutputView(id=oid,artifact=service.ref(row),stage=row['stage'],parents=row['parents'],content=MODELS[row['stage']].model_validate(row['body']),validation=row['validation'],
        review=PlanningReviewView(id=review['id'],reviewer_id=review['reviewer_id'],reviewed_at=review['created_at'],decision=review['decision'],approval_id=review['approval_id'],details=review['details']) if review else None)
    return row,view


@router.get('/artifacts',response_model=list[PlanningOutputView])
def list_artifacts(brand_id:UUID,stage:Stage|None=None,limit:int=Query(20,ge=1,le=50),connection=Depends(actor_transaction)):
    require_brand(connection,brand_id,'internal:read')
    rows=connection.execute('SELECT id FROM mi.planning_outputs WHERE brand_id=%s AND (%s::text IS NULL OR stage=%s) ORDER BY created_at DESC,id DESC LIMIT %s',(brand_id,stage,stage,limit)).fetchall()
    return [output_view(connection,brand_id,r['id'])[1] for r in rows]


@router.get('/artifacts/{output_id}',response_model=PlanningOutputView)
def get_output(brand_id:UUID,output_id:UUID,connection=Depends(actor_transaction)):
    require_brand(connection,brand_id,'internal:read');return output_view(connection,brand_id,output_id)[1]


def validate_decision(ctx,row,body,connection):
    if body.decision!='approved':return
    need(body.evidence_checked and body.dependencies_checked,'planning_human_checks_required')
    need(not any(g.get('critical') for g in row['body'].get('missing_inputs',[])),'planning_critical_gap_unresolved')
    stage=row['stage']
    if stage=='strategy':
        need(body.distinctness_checked and body.selected_option_id is not None,'planning_selection_required')
        critique=service.latest_output(connection,row['brand_id'],'strategy_critique')
        need(critique and critique['body']['strategy_artifact_id']==str(row['artifact_id']) and body.critique_sha256==critique['content_sha256'],'planning_critique_stale')
        need(not any(g.get('critical') for g in critique['body']['missing_inputs']),'planning_critique_gap_unresolved')
        option=next((o for o in row['body']['options'] if o['option_id']==str(body.selected_option_id)),None);need(option,'planning_selected_option_invalid')
        result=next((r for r in critique['body']['reviews'] if r['option_id']==str(body.selected_option_id)),None)
        need(result and result['verdict']=='approve' and not any(i['severity']=='blocking' for i in result['issues']),'planning_blocking_critique')
        acks={a.issue_index:a for a in body.issue_acknowledgements}
        need(len(acks)==len(body.issue_acknowledgements) and set(acks)==set(range(len(result['issues']))) and all(a.accepted_risk for a in acks.values()),'planning_issue_acknowledgements_required')
        service.kpi_checks(body.approved_kpis,require_targets=True)
        expected={k['name']:k for k in option['kpis']};need({k.name for k in body.approved_kpis}==set(expected),'planning_selected_kpis_required')
        for k in body.approved_kpis:
            original=expected[k.name];need(all(k.model_dump(mode='json')[f]==original[f] for f in ['name','definition','unit','measurement_source','review_day']),'planning_kpi_definition_changed')
    elif stage=='thirty_day_plan':need(body.owners_and_schedule_checked and body.budget_checked,'planning_plan_review_checks_required')
    else:need(body.compliance_checked,'planning_compliance_review_required')


@router.post('/artifacts/{output_id}/review',response_model=PlanningOutputView)
def review_output(brand_id:UUID,output_id:UUID,body:PlanningReview,user=Depends(current_user),connection=Depends(actor_transaction)):
    brand=require_brand(connection,brand_id,'internal:read');require_admin(connection,brand['organization_id'])
    w=workflow_for_brand(connection,brand_id,lock=True);row,view=output_view(connection,brand_id,output_id)
    if row['stage'] not in REVIEW_EVENTS:raise HTTPException(409,'A critic verdict is advice; review the strategy options instead')
    latest=service.latest_output(connection,brand_id,row['stage'])
    if latest['id']!=output_id or w['state']!=REVIEW_STATES[row['stage']] or w['version']!=body.expected_workflow_version or body.artifact_sha256!=row['content_sha256']:raise HTTPException(409,'Review must target the latest exact artifact and workflow')
    ctx=checked_context(connection,brand_id,row['stage'])
    if ctx['parents']!=row['parents']:raise HTTPException(409,'Planning inputs changed; regenerate before approval')
    if body.decision=='approved':
        job=connection.execute('SELECT payload FROM mi.jobs WHERE id=%s AND brand_id=%s',(row['job_id'],brand_id)).fetchone()
        _,validation=service.evaluate(row['stage'],json.dumps(row['body']),ctx,job['payload'])
        if not validation.valid:raise HTTPException(409,{'code':'planning_revalidation_failed','issue_codes':validation.issue_codes})
    try:validate_decision(ctx,row,body,connection)
    except PlanningError as exc:raise HTTPException(409,{'code':exc.code})
    approval=uuid4()
    connection.execute("""INSERT INTO mi.approvals(id,organization_id,brand_id,artifact_id,artifact_version,assigned_to,decision,artifact_sha256,requested_by,workflow_id)
        VALUES(%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s)""",(approval,brand['organization_id'],brand_id,row['artifact_id'],row['version'],user['id'],row['content_sha256'],user['id'],w['id']))
    connection.execute('UPDATE mi.approvals SET decision=%s,reason=%s,decided_by=%s,decided_at=now() WHERE id=%s AND brand_id=%s',(body.decision,body.reason,user['id'],approval,brand_id))
    connection.execute('INSERT INTO mi.planning_reviews(organization_id,brand_id,output_id,artifact_sha256,reviewer_id,approval_id,decision,details) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',(brand['organization_id'],brand_id,output_id,row['content_sha256'],user['id'],approval,body.decision,Jsonb(body.model_dump(mode='json'))))
    updates={}
    if body.decision=='approved':
        if row['stage']=='strategy':
            selected=connection.execute('SELECT id FROM mi.strategy_options WHERE artifact_id=%s AND brand_id=%s AND option_key=%s',(row['artifact_id'],brand_id,body.selected_option_id)).fetchone()
            sid=uuid4();connection.execute('INSERT INTO mi.strategy_approvals(id,organization_id,brand_id,approval_id,selected_option_id,strategy_artifact_id,approved_kpis) VALUES(%s,%s,%s,%s,%s,%s,%s)',(sid,brand['organization_id'],brand_id,approval,selected['id'],row['artifact_id'],Jsonb([k.model_dump(mode='json') for k in body.approved_kpis])))
            updates.update(approved_strategy_id=row['artifact_id'],selected_strategy_approval_id=sid)
        elif row['stage']=='thirty_day_plan':updates['approved_plan_id']=row['artifact_id']
        else:updates['approved_creative_id']=row['artifact_id']
    event=REVIEW_EVENTS[row['stage']][0 if body.decision=='approved' else 1]
    transition(connection,w,event,gates(w,human=True,confirmed_profile=True,validated_dossier=True,exact_version_approved=True,red_team_clear=True,kpis_approved=True,dependencies_current=True),body.expected_workflow_version,**updates)
    return output_view(connection,brand_id,output_id)[1]
