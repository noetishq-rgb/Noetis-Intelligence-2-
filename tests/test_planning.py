import copy,json
from uuid import UUID,uuid4
import pytest
from psycopg.types.json import Jsonb
from app.config import settings
from app.worker import transaction
from app.planning_worker import run_once
from app.services.model_provider import ModelResult
from app.services.anthropic_provider import ProviderError
from tests.test_research import research,queue as research_queue,fake_collect,run_view as research_run,human_review
from app.research_worker import run_once as research_once
from tests.test_api_security import invite_user


class PlanningProvider:
    name='recorded'
    model='recorded-phase4-fixture'
    def __init__(self,mode='valid'):self.mode=mode;self.calls=0;self.requests=[]
    def count_tokens(self,request):return 600
    def generate(self,request):
        self.calls+=1;self.requests.append(request)
        if self.mode=='unknown':raise ProviderError('model_outcome_unknown',uncertain=True)
        c=json.loads(request.context_json);stage=request.task_type
        claim=copy.deepcopy(c['approved_claims'][0])
        kpi={'name':'Qualified enquiries','definition':'Enquiries meeting the agreed qualification checklist','unit':'count',
            'baseline':None,'target_min':20,'target_max':40,'measurement_source':'Reviewed lead register','review_day':30}
        if stage=='strategy':
            options=[]
            names=['Capture buyer intent','Teach the everyday use case','Earn the next purchase']
            theses=['Answer comparison queries with a useful evidence-based search experience.','Use accessible educational creative to test an everyday audience hypothesis.','Support known customers with helpful lifecycle messages and a repeat-use offer.']
            offers=['A transparent product comparison','A practical first-use explainer','A repeat-customer starter bundle']
            positioning=['The researched answer at the buying moment','A clear everyday routine without complexity','An easier next choice for existing customers']
            allocs=[{'google_seo':70,'meta_ads':20,'email':10},{'meta_ads':60,'meta_organic':30,'google_seo':10},{'email':70,'meta_organic':20,'meta_ads':10}]
            for i in range(3):
                options.append({'option_id':str(uuid4()),'name':names[i],'thesis':theses[i], 'primary_icp':c['approved_audiences'][0],
                    'core_offer':offers[i],'positioning':positioning[i],'messaging':['A proposed message for testing.'],
                    'channel_allocation':[{'channel':k,'percent':v,'rationale':'Conditional test allocation','evidence':[]} for k,v in allocs[i].items()],
                    'funnel':['Research and brief','Run a controlled '+names[i]+' test','Review qualified outcomes'], 'kpis':[kpi],
                    'expected_revenue_range':None,'forecast_assumptions':[],'required_assets_data':['Human-approved production brief'],
                    'execution_priorities':['Agree a baseline first'],'assumptions':[],'risks':['The proposed use case may not resonate.'],
                    'tradeoffs':['Resources are unavailable for other tests.'],'failure_scenarios':['The first test misses its agreed target.'],
                    'evidence':[claim],'confidence':.65})
            if self.mode=='similar':
                options[1]={**copy.deepcopy(options[0]),'option_id':str(uuid4()),'name':'Different name only'}
            if self.mode=='fabricated':options[0]['evidence'][0]['statement']='This invented statement is not in the approved research.'
            out={'schema_version':'1.1','brand_id':c['brand_id'],'approved_profile_id':c['profile_artifact_id'],'dossier_id':c['dossier_id'],
                'options':options,'recommended_option_id':options[0]['option_id'],'recommendation_reason':'A hypothesis for the human to compare.',
                'contrarian_view':'Existing demand may be too small; validate it before committing.','decision_required':'A human must select an exact option and KPIs.','missing_inputs':[]}
        elif stage=='strategy_critique':
            reviews=[]
            for option in c['strategy']['options']:
                issues=[{'severity':'material','issue':f'Uncertain assumption {i+1}','evidence':[],
                    'required_revision':'Confirm the assumption or record its test and downside.'} for i in range(3)]
                verdict='approve'
                if self.mode=='blocking':issues[0]['severity']='blocking';verdict='revise'
                reviews.append({'option_id':option['option_id'],'verdict':verdict,'issues':issues,
                    'weak_assumptions':['The test might not generalize.'],'missing_evidence':[],
                    'competitor_countermove':'A competitor could test a different offer.','client_questions':['Confirm the priority.'],'risk_score':.4})
            if self.mode=='missing_critique':reviews[-1]['option_id']=reviews[0]['option_id']
            out={'schema_version':'1.0','brand_id':c['brand_id'],'strategy_artifact_id':c['strategy_artifact_id'],'reviews':reviews,'missing_inputs':[]}
        elif stage=='thirty_day_plan':
            ids=[str(uuid4()),str(uuid4())];metric=c['approved_kpis'][0]
            ts=[]
            for i in range(2):
                ts.append({'task_id':ids[i],'day':1+i*6,'channel':'google_seo','deliverable':['Draft a sourced comparison page','Prepare the test variant'][i],
                    'format':'seo_article','audience_segment_id':c['approved_audiences'][0]['segment_id'],'funnel_stage':'consideration',
                    'owner_user_id':c['allowed_owner_ids'][0],'dependency_ids':[] if i==0 else [ids[0]],'approval_required':True,
                    'success_metric':copy.deepcopy(metric),'production_requirements':['Source and editorial review'],'risks':['Claim interpretation needs human review.']})
            if self.mode=='wrong_owner':ts[0]['owner_user_id']=str(uuid4())
            if self.mode=='wrong_kpi':ts[0]['success_metric']['target_max']=999
            out={'schema_version':'1.1','brand_id':c['brand_id'],'approved_strategy_id':c['strategy_artifact_id'],'approval_record_id':c['approval_record_id'],
                'starts_on':c['starts_on'],'duration_days':30,'weekly_themes':[{'week':i,'theme':t} for i,t in enumerate(['Foundation','Build','Test','Learn'],1)],
                'tasks':ts,'test_matrix':[{'hypothesis':'The clearer comparison could improve qualified enquiries.','control':'Current approved experience','variant':'Reviewed comparison variant','success_metric':metric,'checkpoint_days':[14,30]}],
                'budget':c['intake_inputs']['budget_range'],'risks':['The test may not establish causality.'],'missing_inputs':[]}
        else:
            briefs=[]
            for task in c['plan']['tasks']:
                briefs.append({'schema_version':'1.0','brand_id':c['brand_id'],'plan_id':c['plan_id'],'task_id':task['task_id'],
                    'objective':task['deliverable'],'audience':'The approved audience described in current research','channel':task['channel'],'format':task['format'],
                    'key_message':'A proposed editorial comparison supported by the approved evidence.','evidence':[claim], 'hooks':['A useful comparison for an informed decision.'],
                    'copy_variants':[{'name':'Control draft','hook':'A clearer next choice','body':'Draft copy for editorial review.','call_to_action':'Read the reviewed comparison','language':'en','status':'draft'}],
                    'visual_direction':'Use a simple comparison grid with explicit source attribution.','generation_prompts':[],
                    'test_hypothesis':'Clearer information may improve qualified enquiries.','regulated_category_flags':['claims_require_review'],
                    'disclosure_required':True,'upfront_disclosure':'#Ad','compliance_checks':['Human claim and disclosure review'],'assumptions':[], 'missing_inputs':[],'status':'draft'})
            if self.mode=='missing_brief':briefs=briefs[:1]
            out={'schema_version':'1.0','brand_id':c['brand_id'],'plan_id':c['plan_id'],'briefs':briefs}
        return ModelResult(provider=self.name,model=self.model,output_json=json.dumps(out),input_tokens=500,output_tokens=250)


@pytest.fixture
def planning(api,research,service_factory,monkeypatch):
    job=research_queue(api,research);research_once(research['actor'],provider=research['provider'],collector=fake_collect)
    status=research_run(api,research,job);bid=research['bid'];headers=research['agency']['headers']
    d=api.get(f'/v1/brands/{bid}/research/dossiers/{status["dossier_id"]}',headers=headers).json()
    wf=api.get(f'/v1/brands/{bid}/workflow',headers=headers).json()
    reviewed=api.post(f'/v1/brands/{bid}/research/dossiers/{d["id"]}/review',headers=headers,json={'expected_workflow_version':wf['version'],
        'artifact_sha256':d['artifact_sha256'],'decision':'approved','reason':'Recorded research review.','semantic_review':human_review(d['content'])})
    assert reviewed.status_code==200,reviewed.text
    research_connection=settings().worker_database_url
    actor=service_factory(research['brand'],kind='planning');provider=PlanningProvider()
    monkeypatch.setattr('app.planning_api.get_planning_provider',lambda:provider)
    return {**research,'actor':actor,'provider':provider,'headers':headers,'research_actor':research['actor'],'research_connection':research_connection}


def start(api,p,stage,**extra):
    w=api.get(f'/v1/brands/{p["bid"]}/workflow',headers=p['headers']).json()
    body={'stage':stage,'expected_workflow_version':w['version'],**extra}
    result=api.post(f'/v1/brands/{p["bid"]}/planning/runs',headers={**p['headers'],'Idempotency-Key':str(uuid4())},json=body)
    assert result.status_code==202,result.text
    return result.json()


def generate(api,p,stage,**extra):
    queued=start(api,p,stage,**extra)
    assert run_once(p['actor'],provider=p['provider'])
    status=api.get(f'/v1/brands/{p["bid"]}/planning/runs/{queued["job_id"]}',headers=p['headers']).json()
    assert status['status']=='succeeded',status
    output=api.get(f'/v1/brands/{p["bid"]}/planning/artifacts/{status["output_id"]}',headers=p['headers']).json()
    return queued,status,output


def review(api,p,output,**overrides):
    w=api.get(f'/v1/brands/{p["bid"]}/workflow',headers=p['headers']).json()
    body={'expected_workflow_version':w['version'],'artifact_sha256':output['artifact']['sha256'],'decision':'approved','reason':'Recorded human review.',
        'evidence_checked':True,'dependencies_checked':True,'owners_and_schedule_checked':True,'budget_checked':True,'compliance_checked':True}
    body.update(overrides)
    return api.post(f'/v1/brands/{p["bid"]}/planning/artifacts/{output["id"]}/review',headers=p['headers'],json=body)


def select(api,p,strategy,critique):
    option=strategy['content']['options'][0]
    return review(api,p,strategy,distinctness_checked=True,selected_option_id=option['option_id'],critique_sha256=critique['artifact']['sha256'],approved_kpis=option['kpis'],
        issue_acknowledgements=[{'issue_index':i,'rationale':'Human accepts the bounded test risk and will check the assumption.','accepted_risk':True} for i in range(3)])


def selected(api,p):
    _,_,strategy=generate(api,p,'strategy');_,_,critic=generate(api,p,'strategy_critique')
    response=select(api,p,strategy,critic);assert response.status_code==200,response.text
    return strategy,critic


def test_complete_phase4_path_is_human_gated(api,planning,owner):
    p=planning;strategy,critic=selected(api,p)
    _,_,plan=generate(api,p,'thirty_day_plan',starts_on='2026-11-18')
    approved=review(api,p,plan);assert approved.status_code==200,approved.text
    _,_,creative=generate(api,p,'creative_brief')
    approved=review(api,p,creative);assert approved.status_code==200,approved.text
    wf=api.get(f'/v1/brands/{p["bid"]}/workflow',headers=p['headers']).json()
    assert wf['state']=='ready_for_delivery'
    assert p['provider'].calls==4
    assert owner.execute('SELECT count(*) AS n FROM mi.calendar_tasks WHERE brand_id=%s',(p['bid'],)).fetchone()['n']==2
    assert owner.execute('SELECT count(*) AS n FROM mi.creative_briefs WHERE brand_id=%s',(p['bid'],)).fetchone()['n']==2
    assert [r.task_type for r in p['provider'].requests]==['strategy','strategy_critique','thirty_day_plan','creative_brief']


def test_strategy_needs_approved_research(api,research,monkeypatch):
    monkeypatch.setattr('app.planning_api.get_planning_provider',lambda:PlanningProvider())
    p={**research,'headers':research['agency']['headers']}
    wf=api.get(f'/v1/brands/{p["bid"]}/workflow',headers=p['headers']).json()
    result=api.post(f'/v1/brands/{p["bid"]}/planning/runs',headers={**p['headers'],'Idempotency-Key':str(uuid4())},json={'stage':'strategy','expected_workflow_version':wf['version']})
    assert result.status_code==409 and result.json()['detail']['code']=='planning_approved_research_required'


def status(api,p,job):
    response=api.get(f'/v1/brands/{p["bid"]}/planning/runs/{job["job_id"]}',headers=p['headers'])
    assert response.status_code==200,response.text
    return response.json()


def retry_now(p,job):
    with transaction(p['actor']) as c:c.execute('UPDATE mi.jobs SET available_at=now() WHERE id=%s AND brand_id=%s',(job['job_id'],p['bid']))


@pytest.mark.parametrize('mode,code',[('similar','planning_options_too_similar'),('fabricated','planning_fact_not_approved')])
def test_invalid_strategies_stop_after_two_attempts(api,planning,mode,code,owner):
    p=planning;p['provider'].mode=mode;job=start(api,p,'strategy')
    run_once(p['actor'],provider=p['provider']);first=status(api,p,job)
    assert first['status']=='retry' and first['latest_validation']['issue_codes']==[code]
    retry_now(p,job);run_once(p['actor'],provider=p['provider']);second=status(api,p,job)
    assert second['status']=='failed' and second['output_id'] is None
    assert not run_once(p['actor'],provider=p['provider']) and p['provider'].calls==2
    assert api.get(f'/v1/brands/{p["bid"]}/workflow',headers=p['headers']).json()['state']=='needs_human_review'
    assert owner.execute("SELECT count(*) AS n FROM mi.agent_outputs WHERE brand_id=%s AND output_schema='strategy-options-v1.1'",(p['bid'],)).fetchone()['n']==2


def test_critique_must_cover_every_option_once(api,planning):
    p=planning;generate(api,p,'strategy');p['provider'].mode='missing_critique';job=start(api,p,'strategy_critique')
    run_once(p['actor'],provider=p['provider']);result=status(api,p,job)
    assert result['latest_validation']['issue_codes']==['planning_critique_coverage_invalid'] and result['output_id'] is None


def test_blocking_critique_prevents_selection_and_revision_is_recorded(api,planning):
    p=planning;_,_,strategy=generate(api,p,'strategy');p['provider'].mode='blocking';_,_,critic=generate(api,p,'strategy_critique')
    blocked=select(api,p,strategy,critic)
    assert blocked.status_code==409 and blocked.json()['detail']['code']=='planning_blocking_critique'
    result=review(api,p,strategy,decision='revision_requested',reason='Resolve the blocking assumption and revise the options.')
    assert result.status_code==200,result.text
    p['provider'].mode='valid';job=start(api,p,'strategy');run_once(p['actor'],provider=p['provider'])
    assert 'Resolve the blocking' in p['provider'].requests[-1].context_json


def test_selection_binds_critique_hash_acknowledgements_and_kpis(api,planning):
    p=planning;_,_,strategy=generate(api,p,'strategy');_,_,critic=generate(api,p,'strategy_critique');option=strategy['content']['options'][0]
    bad=review(api,p,strategy,distinctness_checked=True,selected_option_id=option['option_id'],critique_sha256='a'*64,approved_kpis=option['kpis'])
    assert bad.status_code==409 and bad.json()['detail']['code']=='planning_critique_stale'
    missing=review(api,p,strategy,distinctness_checked=True,selected_option_id=option['option_id'],critique_sha256=critic['artifact']['sha256'],approved_kpis=option['kpis'])
    assert missing.status_code==409 and missing.json()['detail']['code']=='planning_issue_acknowledgements_required'
    good=select(api,p,strategy,critic);assert good.status_code==200,good.text
    assert select(api,p,strategy,critic).status_code==409


@pytest.mark.parametrize('mode,code',[('wrong_owner','planning_owner_not_authorized'),('wrong_kpi','planning_kpi_not_approved')])
def test_plan_cannot_reassign_owners_or_change_approved_targets(api,planning,mode,code):
    p=planning;selected(api,p);p['provider'].mode=mode
    job=start(api,p,'thirty_day_plan',starts_on='2026-11-18');run_once(p['actor'],provider=p['provider'])
    result=status(api,p,job);assert result['latest_validation']['issue_codes']==[code] and result['output_id'] is None


def test_creative_batch_requires_all_tasks_and_human_plan_approval(api,planning):
    p=planning;selected(api,p);_,_,plan=generate(api,p,'thirty_day_plan',starts_on='2026-11-18')
    wf=api.get(f'/v1/brands/{p["bid"]}/workflow',headers=p['headers']).json()
    before=api.post(f'/v1/brands/{p["bid"]}/planning/runs',headers={**p['headers'],'Idempotency-Key':str(uuid4())},json={'stage':'creative_brief','expected_workflow_version':wf['version']})
    assert before.status_code==409
    assert review(api,p,plan,owners_and_schedule_checked=False).status_code==409
    assert review(api,p,plan).status_code==200
    p['provider'].mode='missing_brief';job=start(api,p,'creative_brief');run_once(p['actor'],provider=p['provider'])
    result=status(api,p,job);assert result['latest_validation']['issue_codes']==['planning_creative_coverage_invalid']


def test_client_and_wrong_brand_cannot_read_or_review_outputs(api,planning,brand_factory):
    p=planning;_,_,out=generate(api,p,'strategy')
    other=brand_factory(p['agency'])['brand']['id']
    assert api.get(f'/v1/brands/{other}/planning/artifacts/{out["id"]}',headers=p['headers']).status_code==404
    client=invite_user(api,p['agency'],p['brand'],'client')
    for path in ['/artifacts','/owners',f'/artifacts/{out["id"]}']:
        assert api.get(f'/v1/brands/{p["bid"]}/planning'+path,headers=client).status_code==404
    denied=api.post(f'/v1/brands/{p["bid"]}/planning/artifacts/{out["id"]}/review',headers=client,json={'expected_workflow_version':1,'artifact_sha256':out['artifact']['sha256'],'decision':'approved','reason':'Not assigned internal access'})
    assert denied.status_code==404


def test_planning_service_cannot_write_a_human_review(api,planning):
    import psycopg
    p=planning;_,_,out=generate(api,p,'strategy')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with transaction(p['actor']) as c:
            c.execute("INSERT INTO mi.planning_reviews(organization_id,brand_id,output_id,artifact_sha256,reviewer_id,approval_id,decision,details) VALUES(%s,%s,%s,%s,%s,%s,'approved',%s)",
                (p['brand']['brand']['organization_id'],p['bid'],out['id'],out['artifact']['sha256'],p['actor'],uuid4(),Jsonb({})))


def test_planning_cost_cap_stops_before_generation(api,planning,monkeypatch):
    p=planning;monkeypatch.setattr(settings(),'model_max_cost_paise',1)
    job=start(api,p,'strategy');run_once(p['actor'],provider=p['provider'])
    result=status(api,p,job);assert result['status']=='failed' and p['provider'].calls==0
    assert result['last_error']=='research_cost_cap_reached'


def test_uncertain_generation_is_not_automatically_replayed(api,planning):
    p=planning;p['provider'].mode='unknown';job=start(api,p,'strategy')
    run_once(p['actor'],provider=p['provider']);result=status(api,p,job)
    assert result['status']=='failed' and result['tokens_reserved']>0 and p['provider'].calls==1
    assert not run_once(p['actor'],provider=p['provider'])
    w=api.get(f'/v1/brands/{p["bid"]}/workflow',headers=p['headers']).json()
    body={'stage':'strategy','expected_workflow_version':w['version']}
    url=f'/v1/brands/{p["bid"]}/planning/runs'
    assert api.post(url,headers={**p['headers'],'Idempotency-Key':str(uuid4())},json=body).status_code==409
    accepted=api.post(url,headers={**p['headers'],'Idempotency-Key':str(uuid4())},json={**body,'acknowledge_uncertain_charge':True})
    assert accepted.status_code==202,accepted.text


def test_cancelled_generation_keeps_usage_without_applying_an_artifact(api,planning,owner):
    p=planning;original=p['provider'].generate
    def cancelling(request):
        wf=api.get(f'/v1/brands/{p["bid"]}/workflow',headers=p['headers']).json()
        result=api.post(f'/v1/brands/{p["bid"]}/workflow/events',headers=p['headers'],json={'event':'cancel','expected_version':wf['version']})
        assert result.status_code==200,result.text
        return original(request)
    p['provider'].generate=cancelling;job=start(api,p,'strategy');run_once(p['actor'],provider=p['provider']);result=status(api,p,job)
    assert result['status']=='cancelled' and result['output_id'] is None and result['tokens_reserved']==0
    assert result['tokens_used']==1300 # 550 research + 750 observed strategy usage
    raw=owner.execute("SELECT body FROM mi.agent_outputs WHERE brand_id=%s AND output_schema='strategy-options-v1.1'",(p['bid'],)).fetchone()
    assert raw and raw['body']['validation']['semantic_review_required'] is True


def test_known_usage_overshoot_pauses_and_records_actual_tokens(api,planning):
    p=planning;original=p['provider'].generate
    p['provider'].generate=lambda request:original(request).model_copy(update={'output_tokens':10000})
    job=start(api,p,'strategy');run_once(p['actor'],provider=p['provider']);result=status(api,p,job)
    assert result['status']=='failed' and result['output_id'] is None
    assert result['tokens_used']==11050 and result['last_error']=='provider_usage_exceeded_reservation'


def test_research_usage_accounting_remains_authorized_after_strategy_starts(api,planning):
    from app.db import transaction as api_transaction
    from app.services.research_budget import budget_context
    p=planning;start(api,p,'strategy')
    with api_transaction(p['research_actor'],p['research_connection'].get_secret_value()) as c:
        assert not c.execute("SELECT mi.has_brand_scope(%s,'planning:run') AS yes",(p['bid'],)).fetchone()['yes']
        budget_context(c)
        updated=c.execute('UPDATE mi.workflow_instances SET tokens_used=tokens_used WHERE brand_id=%s RETURNING id',(p['bid'],)).fetchone()
        assert updated


def test_planning_is_disabled_by_default_even_with_approved_research(api,planning,monkeypatch):
    from app.services.anthropic_provider import get_planning_provider
    p=planning;monkeypatch.setattr(settings(),'allow_planning_calls',False)
    monkeypatch.setattr('app.planning_api.get_planning_provider',get_planning_provider)
    w=api.get(f'/v1/brands/{p["bid"]}/workflow',headers=p['headers']).json()
    response=api.post(f'/v1/brands/{p["bid"]}/planning/runs',headers={**p['headers'],'Idempotency-Key':str(uuid4())},json={'stage':'strategy','expected_workflow_version':w['version']})
    assert response.status_code==503 and response.json()['detail']=='planning_model_not_enabled'
    assert p['provider'].calls==0
