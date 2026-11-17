import json
from datetime import date
from uuid import UUID,uuid4
import pytest
from app.config import settings
from app.db import transaction
from app.research_models import AREAS, SourceSeed
from app.research_worker import run_once
from app.services.model_provider import ModelResult
from app.services.research_sources import CollectedSource
from app.services.anthropic_provider import ProviderError
from tests.test_api_security import fake_crawl, invite_user
from app.services.research_validation import claims_in
from app.research_models import ResearchDossierV11

TEXT='Customers in India ask for transparent pricing before booking. The captured page describes a service and its stated audience.'


def human_review(content):
    dossier=ResearchDossierV11.model_validate(content)
    return {'source_quality_checked':True,'geography_checked':True,'contradictions_checked':True,'coverage_checked':True,
        'claim_assessments':[{'claim_id':str(c.claim_id),'verdict':'supported','source_ids':[str(s) for s in c.source_ids],
            'rationale':'Recorded human assessment: the finding describes the supplied page observation, with its stated sample limits.'}
            for c in claims_in(dossier) if c.classification in {'fact','inference'}], 'unresolved_issues':[]}


def fake_collect(seeds,queries,trace=None):
    result=[]
    for seed in seeds:
        if trace:
            trace({'endpoint':str(seed.url),'response_status':200,'error_code':None,'latency_ms':1})
        result.append(CollectedSource(seed,str(seed.url),'example.com',None,TEXT,'a'*64,.85,[]))
    return result,[]


class RecordedProvider:
    name='recorded'
    model='recorded-research-fixture'
    def __init__(self,mode='valid',count=500):
        self.mode,self.count=mode,count
        self.calls=0
        self.requests=[]
    def count_tokens(self,request):
        return self.count
    def generate(self,request):
        self.calls+=1
        self.requests.append(request)
        if self.mode=='unknown':
            raise ProviderError('model_outcome_unknown',uncertain=True)
        ctx=json.loads(request.context_json)
        evidence=ctx['evidence']
        claim_by_area={}
        support=[]
        for item in evidence:
            claim_id=str(uuid4())
            claim={'claim_id':claim_id,'classification':'fact','statement':'The sampled page states: '+TEXT,
                'source_ids':[item['citation']['source_id']],'confidence':.7,'rationale':'A captured page observation, not a representative market statistic.','geographic_scope':'india'}
            claim_by_area[item['area']]=claim
            support.append({'claim_id':claim_id,'source_id':item['citation']['source_id'],'quote':TEXT})
        gaps=[]
        coverage=[]
        for area in AREAS:
            known=area in claim_by_area
            coverage.append({'area':area,'status':'supported' if known else 'unavailable','finding_ids':[claim_by_area[area]['claim_id']] if known else [],'limitations':[] if known else ['No eligible source captured for this area.']})
            if not known:
                gaps.append({'field':area,'question':'Provide evidence for '+area,'critical':False,'reason':'Not researched in this bounded fixture.'})
        payload={'schema_version':'1.1','brand_id':ctx['brand_id'],'profile_version':ctx['profile_version'],
            'executive_summary':'Recorded fixture summary. Findings reflect only the captured pages.',
            'geographic_focus':'IN','research_as_of':ctx.get('research_as_of',date.today().isoformat()),
            'sources':[item['citation'] for item in evidence],
            'brand_findings':[claim_by_area['brand']] if 'brand' in claim_by_area else [],
            'category_market':[claim_by_area['market']] if 'market' in claim_by_area else [],
            'demand_seasonality':[],'audiences':[], 'competitors':[], 'social_listening':[], 'reputation_reviews':[],
            'search_opportunities':[],'creative_content_gaps':[],'regulatory_cultural_risks':[], 'contradictions':[],
            'missing_inputs':gaps,'client_questions':['Supply the outstanding research inputs.'],
            'next_action':'A founder should review the evidence and the coverage limitations.',
            'claim_support':support,'coverage':coverage,'conflict_pairs':[]}
        if 'audience' in claim_by_area:
            payload['audiences']=[{'segment_id':str(uuid4()),'name':'Sample audience','geography':['India'],'languages':['en'],
                'needs':[claim_by_area['audience']],'objections':[],'buying_triggers':[],'evidence':[]}]
        if self.mode=='bad_quote':
            payload['claim_support'][0]['quote']='This quotation does not occur in the source text.'
        if self.mode=='critical':
            payload['missing_inputs'].append({'field':'budget_range','question':'Confirm the campaign budget.','critical':True,'reason':'Requested for a future decision.'})
        if self.mode=='conflict':
            claims=[claim_by_area['brand']['claim_id'],claim_by_area['market']['claim_id']]
            payload['conflict_pairs']=[{'conflict_id':str(uuid4()),'claim_ids':claims,'explanation':'Recorded contradiction requiring a human choice.','critical':True}]
        return ModelResult(provider=self.name,model=self.model,output_json=json.dumps(payload),input_tokens=400,output_tokens=150)


@pytest.fixture
def research(api,agency,brand_factory,service_factory,monkeypatch):
    from app.worker import run_once as crawl_once
    brand=brand_factory(agency)
    bid=brand['brand']['id']
    crawler=service_factory(brand)
    assert crawl_once(crawler,fake_crawl)
    answers={'business_description':'A service business in India.','audience_description':'Business owners in India.',
        'primary_objective':'Generate qualified enquiries','budget_range':{'minimum':{'amount_paise':1000000},'maximum':{'amount_paise':2000000}}}
    draft=api.put(f'/v1/brands/{bid}/onboarding',headers=agency['headers'],json={'expected_version':0,'answers':answers})
    assert draft.status_code==200,draft.text
    submitted=api.post(f'/v1/brands/{bid}/onboarding/submit',headers=agency['headers'],json={'expected_version':draft.json()['version']})
    assert submitted.status_code==200,submitted.text
    profile=api.get(f'/v1/brands/{bid}/profile',headers=agency['headers']).json()
    confirmed=api.post(f'/v1/brands/{bid}/profile/confirm',headers=agency['headers'],json={'expected_profile_version':profile['version'],'onboarding_response_id':submitted.json()['id']})
    assert confirmed.status_code==200,confirmed.text
    actor=service_factory(brand,kind='research')
    provider=RecordedProvider()
    monkeypatch.setattr('app.research_api.get_model_provider',lambda:provider)
    config=settings()
    monkeypatch.setattr(config,'model_input_paise_per_million',1000000)
    monkeypatch.setattr(config,'model_output_paise_per_million',2000000)
    monkeypatch.setattr(config,'pricing_as_of','2026-11-16')
    return {'brand':brand,'bid':bid,'actor':actor,'provider':provider,'agency':agency}


def queue(api,research,**overrides):
    bid=research['bid']
    headers=research['agency']['headers']
    wf=api.get(f'/v1/brands/{bid}/workflow',headers=headers).json()
    body={'expected_workflow_version':wf['version'],'sources':[{'url':'https://example.com/'+area,'area':area,'geographic_scope':'india'} for area in ['brand','market','audience']]}
    body.update(overrides)
    result=api.post(f'/v1/brands/{bid}/research/runs',headers={**headers,'Idempotency-Key':str(uuid4())},json=body)
    assert result.status_code==202,result.text
    return result.json()


def run_view(api,research,job):
    result=api.get(f"/v1/brands/{research['bid']}/research/runs/{job['job_id']}",headers=research['agency']['headers'])
    assert result.status_code==200,result.text
    return result.json()


def test_research_end_to_end_preserves_evidence_usage_and_review(api,research,owner):
    job=queue(api,research)
    assert run_once(research['actor'],provider=research['provider'],collector=fake_collect)
    state=run_view(api,research,job)
    assert state['status']=='succeeded',state
    assert state['source_count']==3
    assert state['tokens_used']==550 and state['tokens_reserved']==0
    assert state['cost_used_paise']==700
    assert state['latest_validation']['schema_status']=='passed'
    assert state['latest_validation']['provenance_status']=='passed'
    assert state['latest_validation']['semantic_status']=='not_assessed'
    bid=research['bid']; headers=research['agency']['headers']
    sources=api.get(f"/v1/brands/{bid}/research/runs/{job['job_id']}/sources",headers=headers)
    assert sources.status_code==200 and len(sources.json())==3
    assert all(item['excerpt']==TEXT for item in sources.json())
    initialized=api.post(f'/v1/brands/{bid}/research/templates/initialize',headers=headers)
    assert initialized.status_code==200
    dossier=api.get(f"/v1/brands/{bid}/research/dossiers/{state['dossier_id']}",headers=headers).json()
    assert dossier['content']['schema_version']=='1.1'
    wf=api.get(f'/v1/brands/{bid}/workflow',headers=headers).json()
    result=api.post(f"/v1/brands/{bid}/research/dossiers/{dossier['id']}/review",headers=headers,json={
        'expected_workflow_version':wf['version'],'artifact_sha256':dossier['artifact_sha256'],'decision':'approved','reason':'Reviewed the captured claims, quotes and gaps.',
        'semantic_review':human_review(dossier['content'])})
    assert result.status_code==200,result.text
    assert result.json()['review_record']['decision']=='approved'
    assert result.json()['semantic_review_status']=='approved'
    assert result.json()['review_record']['semantic_review']['claim_assessments']
    assert api.get(f'/v1/brands/{bid}/workflow',headers=headers).json()['state']=='ready_for_strategy'
    prompt=owner.execute('SELECT body FROM mi.prompt_runs WHERE brand_id=%s',(bid,)).fetchone()['body']
    assert prompt['usage']['input_tokens']==400 and prompt['cost']['currency']=='INR'
    assert owner.execute('SELECT count(*) AS n FROM mi.agent_outputs WHERE brand_id=%s AND valid',(bid,)).fetchone()['n']==1


def test_invalid_quote_twice_stops_automatic_retries(api,research,owner):
    research['provider'].mode='bad_quote'
    job=queue(api,research)
    assert run_once(research['actor'],provider=research['provider'],collector=fake_collect)
    state=run_view(api,research,job)
    assert state['status']=='retry'
    assert state['latest_validation']['issue_codes']==['research_quote_missing']
    with transaction(research['actor']) as c:
        c.execute("UPDATE mi.jobs SET available_at=now() WHERE id=%s AND brand_id=%s",(job['job_id'],research['bid']))
    assert run_once(research['actor'],provider=research['provider'],collector=fake_collect)
    state=run_view(api,research,job)
    assert state['status']=='failed' and state['dossier_id'] is None
    wf=api.get(f"/v1/brands/{research['bid']}/workflow",headers=research['agency']['headers']).json()
    assert wf['state']=='needs_human_review' and wf['validation_failures']==2
    assert research['provider'].calls==2
    assert not run_once(research['actor'],provider=research['provider'],collector=fake_collect)
    assert owner.execute('SELECT count(*) AS n FROM mi.agent_outputs WHERE brand_id=%s AND NOT valid',(research['bid'],)).fetchone()['n']==2


def test_unknown_generation_is_not_replayed_and_retains_reservation(api,research,owner):
    research['provider'].mode='unknown'
    job=queue(api,research)
    assert run_once(research['actor'],provider=research['provider'],collector=fake_collect)
    state=run_view(api,research,job)
    assert state['status']=='failed' and state['tokens_reserved']>0 and state['tokens_used']==0
    assert not run_once(research['actor'],provider=research['provider'],collector=fake_collect)
    assert research['provider'].calls==1
    dispatch=owner.execute('SELECT state,input_tokens FROM mi.model_dispatches WHERE job_id=%s',(job['job_id'],)).fetchone()
    assert dispatch['state']=='outcome_unknown' and dispatch['input_tokens'] is None
    wf=api.get(f"/v1/brands/{research['bid']}/workflow",headers=research['agency']['headers']).json()
    body={'expected_workflow_version':wf['version'],'sources':[{'url':'https://example.com','area':'brand'}]}
    response=api.post(f"/v1/brands/{research['bid']}/research/runs",headers={**research['agency']['headers'],'Idempotency-Key':str(uuid4())},json=body)
    assert response.status_code==409
    response=api.post(f"/v1/brands/{research['bid']}/research/runs",headers={**research['agency']['headers'],'Idempotency-Key':str(uuid4())},json={**body,'acknowledge_uncertain_charge':True})
    assert response.status_code==202,response.text


def review_request(api,research,state,**extra):
    bid=research['bid'];headers=research['agency']['headers']
    dossier=api.get(f"/v1/brands/{bid}/research/dossiers/{state['dossier_id']}",headers=headers).json()
    wf=api.get(f'/v1/brands/{bid}/workflow',headers=headers).json()
    body={'expected_workflow_version':wf['version'],'artifact_sha256':dossier['artifact_sha256'],'decision':'approved','reason':'Recorded founder review.',
        'semantic_review':human_review(dossier['content'])}
    body.update(extra)
    response=api.post(f"/v1/brands/{bid}/research/dossiers/{dossier['id']}/review",headers=headers,json=body)
    return response,dossier


def test_critical_gap_requires_explicit_founder_answer(api,research):
    research['provider'].mode='critical'
    job=queue(api,research)
    run_once(research['actor'],provider=research['provider'],collector=fake_collect)
    state=run_view(api,research,job)
    result,dossier=review_request(api,research,state)
    assert result.status_code==409
    result,_=review_request(api,research,state,gap_resolutions=[{'field':'budget_range','answer':'The founder confirms the current intake budget remains applicable.','kind':'client_input','source_ids':[]}])
    assert result.status_code==200,result.text


def test_critical_contradiction_and_stale_hash_block_approval(api,research):
    research['provider'].mode='conflict'
    job=queue(api,research)
    run_once(research['actor'],provider=research['provider'],collector=fake_collect)
    state=run_view(api,research,job)
    result,dossier=review_request(api,research,state)
    assert result.status_code==409
    result,_=review_request(api,research,state,artifact_sha256='0'*64)
    assert result.status_code==409
    conflict=dossier['content']['conflict_pairs'][0]
    claim=dossier['content']['brand_findings'][0]
    result,_=review_request(api,research,state,conflict_resolutions=[{'conflict_id':conflict['conflict_id'],
        'selected_claim_id':claim['claim_id'],'rationale':'Founder reviewed the captured source and recorded this choice.', 'source_ids':claim['source_ids']}])
    assert result.status_code==200,result.text


def test_budget_limit_prevents_generation(api,research,monkeypatch):
    monkeypatch.setattr(settings(),'research_max_cost_paise',1)
    job=queue(api,research)
    run_once(research['actor'],provider=research['provider'],collector=fake_collect)
    state=run_view(api,research,job)
    assert state['status']=='failed' and state['model_calls']==0 and research['provider'].calls==0
    assert state['last_error']=='research_cost_cap_reached'


def test_client_cannot_access_dossiers_runs_or_raw_prompts(api,research):
    job=queue(api,research)
    run_once(research['actor'],provider=research['provider'],collector=fake_collect)
    state=run_view(api,research,job)
    headers=invite_user(api,research['agency'],research['brand'],'client')
    bid=research['bid']
    for route in ['/dossiers',f"/dossiers/{state['dossier_id']}",f"/runs/{job['job_id']}"]:
        assert api.get(f'/v1/brands/{bid}/research'+route,headers=headers).status_code==404
    result=api.post(f'/v1/brands/{bid}/research/prompts/compile',headers=headers,json={'evidence_ids':[str(uuid4())]})
    assert result.status_code==404


def test_research_cannot_start_before_confirmed_intake_or_without_provider(api,agency,brand_factory,monkeypatch):
    brand=brand_factory(agency);bid=brand['brand']['id']
    monkeypatch.setattr('app.research_api.get_model_provider',lambda:RecordedProvider())
    wf=api.get(f'/v1/brands/{bid}/workflow',headers=agency['headers']).json()
    body={'expected_workflow_version':wf['version'],'sources':[{'url':'https://example.com'}]}
    response=api.post(f'/v1/brands/{bid}/research/runs',headers={**agency['headers'],'Idempotency-Key':str(uuid4())},json=body)
    assert response.status_code==409
    def unavailable(): raise ProviderError('model_not_configured')
    monkeypatch.setattr('app.research_api.get_model_provider',unavailable)
    response=api.post(f'/v1/brands/{bid}/research/runs',headers={**agency['headers'],'Idempotency-Key':str(uuid4())},json=body)
    assert response.status_code==503


def test_no_usable_source_pauses_before_model_generation(api,research):
    job=queue(api,research)
    def unavailable(seeds,queries,trace=None):return [],['Robots policy prevented access.']
    run_once(research['actor'],provider=research['provider'],collector=unavailable)
    state=run_view(api,research,job)
    assert state['status']=='failed' and state['source_count']==0 and state['model_calls']==0
    assert research['provider'].calls==0
    wf=api.get(f"/v1/brands/{research['bid']}/workflow",headers=research['agency']['headers']).json()
    assert wf['state']=='awaiting_inputs'


def test_crash_after_dispatch_pauses_instead_of_duplicate_call(api,research):
    from app.research_worker import claim,capture,prepare,finish,transaction as worker_transaction
    from app.services.research_budget import reserve
    from app.repository import workflow_for_brand
    queued=queue(api,research)
    job=claim(research['actor'])
    assert capture(research['actor'],job,fake_collect)
    request,profile,evidence=prepare(research['actor'],job)
    with worker_transaction(research['actor']) as c:
        wf=workflow_for_brand(c,research['bid'],lock=True)
        dispatch=reserve(c,job,wf,profile,request,500,'recorded','fixture-model',research['actor'])
        c.execute("UPDATE mi.model_dispatches SET state='dispatched',dispatched_at=now() WHERE id=%s AND brand_id=%s",(dispatch['id'],research['bid']))
        c.execute("UPDATE mi.jobs SET lease_expires_at=now()-interval '1 second' WHERE id=%s AND brand_id=%s",(job['id'],research['bid']))
    assert not run_once(research['actor'],provider=research['provider'],collector=fake_collect)
    status=run_view(api,research,queued)
    assert status['last_error']=='worker_lost_after_dispatch' and status['tokens_reserved']>0
    assert research['provider'].calls==0
    # A delayed result settles observed usage but cannot apply an expired lease's output.
    result=research['provider'].generate(request)
    finish(research['actor'],job,dispatch,request,result,profile,evidence,[],1)
    status=run_view(api,research,queued)
    assert status['tokens_reserved']==0 and status['tokens_used']==550
    assert status['cost_used_paise']==700 and status['dossier_id'] is None
    assert status['uncertain_dispatches']==0
    from app.services.research_budget import settle
    with worker_transaction(research['actor']) as c:
        workflow_for_brand(c,research['bid'],lock=True)
        settle(c,dispatch,result,latency_ms=1)
    assert run_view(api,research,queued)['tokens_used']==550


def test_cancellation_keeps_usage_and_raw_response_but_discards_dossier(api,research,owner):
    queued=queue(api,research)
    class CancellingProvider(RecordedProvider):
        def generate(self,request):
            wf=api.get(f"/v1/brands/{research['bid']}/workflow",headers=research['agency']['headers']).json()
            result=api.post(f"/v1/brands/{research['bid']}/workflow/events",headers=research['agency']['headers'],json={'expected_version':wf['version'],'event':'cancel'})
            assert result.status_code==200,result.text
            return super().generate(request)
    provider=CancellingProvider()
    run_once(research['actor'],provider=provider,collector=fake_collect)
    status=run_view(api,research,queued)
    assert status['status']=='cancelled' and status['dossier_id'] is None
    assert status['tokens_used']==550 and status['tokens_reserved']==0
    row=owner.execute('SELECT body FROM mi.prompt_runs WHERE brand_id=%s',(research['bid'],)).fetchone()['body']
    assert row['error_code']=='discarded_after_cancellation'
    assert owner.execute('SELECT count(*) AS n FROM mi.agent_outputs WHERE brand_id=%s',(research['bid'],)).fetchone()['n']==1


def test_compile_rejects_cross_brand_evidence_and_excludes_private_fields(api,research,brand_factory,owner):
    queued=queue(api,research)
    run_once(research['actor'],provider=research['provider'],collector=fake_collect)
    ids=[str(row['id']) for row in owner.execute('SELECT id FROM mi.research_evidence WHERE job_id=%s',(queued['job_id'],)).fetchall()]
    other=brand_factory(research['agency'])['brand']['id']
    headers=research['agency']['headers']
    response=api.post(f'/v1/brands/{other}/research/prompts/compile',headers=headers,json={'evidence_ids':ids})
    assert response.status_code==404
    response=api.post(f"/v1/brands/{research['bid']}/research/prompts/compile",headers=headers,json={'evidence_ids':ids,'task_type':'competitor_research'})
    assert response.status_code==200,response.text
    context=json.loads(response.json()['context_json'])
    assert 'budget_range' not in context['brand'] and 'pricing_margin_notes' not in context['brand']
    assert 'objectives' not in context['brand']


def test_new_approval_requires_explicit_semantic_review_and_supports_revision(api,research):
    queued=queue(api,research)
    run_once(research['actor'],provider=research['provider'],collector=fake_collect)
    state=run_view(api,research,queued)
    response,dossier=review_request(api,research,state,semantic_review=None)
    assert response.status_code==409
    assert response.json()['detail']['code']=='review_semantic_required'
    semantic=human_review(dossier['content'])
    semantic['claim_assessments'][0]['verdict']='unsupported'
    semantic['claim_assessments'][0]['rationale']='This real quotation does not support the claim meaning.'
    response,_=review_request(api,research,state,semantic_review=semantic)
    assert response.status_code==409 and response.json()['detail']['code']=='review_semantic_unresolved'
    response,_=review_request(api,research,state,decision='revision_requested',semantic_review=semantic)
    assert response.status_code==200,response.text
    assert response.json()['semantic_review_status']=='revision_requested'
    wf=api.get(f"/v1/brands/{research['bid']}/workflow",headers=research['agency']['headers']).json()
    assert wf['state']=='ready_for_research'


def test_database_rejects_approval_that_omits_semantic_evidence(api,research,owner):
    import psycopg
    from psycopg.types.json import Jsonb
    queued=queue(api,research)
    run_once(research['actor'],provider=research['provider'],collector=fake_collect)
    state=run_view(api,research,queued)
    dossier=api.get(f"/v1/brands/{research['bid']}/research/dossiers/{state['dossier_id']}",headers=research['agency']['headers']).json()
    actor=owner.execute('SELECT id FROM identity.users WHERE email=%s',(research['agency']['email'],)).fetchone()['id']
    with pytest.raises(psycopg.errors.CheckViolation):
        with transaction(actor) as c:
            c.execute("""INSERT INTO mi.research_reviews(organization_id,brand_id,dossier_id,artifact_id,artifact_sha256,
                reviewer_id,decision,reason,resolutions) VALUES(%s,%s,%s,%s,%s,mi.actor_id(),'approved','Missing semantic evidence',%s)""",
                (research['agency']['organization'],research['bid'],dossier['id'],dossier['artifact_id'],dossier['artifact_sha256'],Jsonb({})))
