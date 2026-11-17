from uuid import UUID,uuid4
import pytest
from app.config import settings
from app.services.crawl import CrawledPage
from app.worker import run_once as crawl_once
from tests.test_client_access import issued,login_and_change
from tests.test_research import RecordedProvider,TEXT


def rich_draft():
    return {'research_intake_version':1,'brand_name':'Fresh client brand','category':'Food & beverage',
        'objectives':['Brand awareness','Lead generation'],'online_presence':[{'url':'https://example.com/','platform':'website'}],
        'business_description':'Protein snacks for active customers.','audience_description':'Adults seeking convenient protein snacks.',
        'target_geographies':['India'],'primary_objective':'Brand awareness',
        'budget_range':{'minimum':{'amount_paise':1000000},'maximum':{'amount_paise':2000000}}}


def test_rich_submission_capture_human_confirmation_and_pilot_are_connected(api,issued,owner,service_factory,monkeypatch):
    client=login_and_change(api,issued);bid=issued['account']['brand_id'];base=f'/v1/brands/{bid}/brain';h=issued['owner_headers']
    # An owner can open the empty workspace first; submission still belongs to
    # the actual client who later completes the form.
    session=api.post(base+'/sessions',headers=h,json={'research_intake_version':1}).json()
    incomplete=api.post(base+'/sessions/'+session['id']+'/submit',headers=client,json={'expected_version':session['version']})
    assert incomplete.status_code==422
    saved=api.put(base+'/sessions/'+session['id'],headers=client,json={'expected_version':session['version'],'draft':rich_draft()}).json()
    submitted=api.post(base+'/sessions/'+session['id']+'/submit',headers=client,json={'expected_version':saved['version']})
    assert submitted.status_code==200,submitted.text
    assert submitted.json()['onboarding_response_id']
    assert owner.execute("SELECT count(*) AS n FROM mi.onboarding_responses WHERE brand_id=%s AND status='submitted'",(bid,)).fetchone()['n']==1
    assert api.post(base+'/sessions/'+session['id']+'/submit',headers=client,json={'expected_version':submitted.json()['version']}).status_code==200
    progress=api.get(base+'/sessions/'+session['id']+'/progress',headers=client).json()
    assert progress['pending_action']=='capture_website' and progress['missing_research_inputs']==[]
    capture=api.post(base+'/sessions/'+session['id']+'/capture',headers={**h,'Idempotency-Key':str(uuid4())},json={'expected_version':submitted.json()['version']})
    assert capture.status_code==202,capture.text
    current=capture.json()['session']
    actor=service_factory({'brand':{'id':bid,'organization_id':issued['organization']}})
    def website(url,max_pages,trace=None):return [CrawledPage(url,200,'Public source','A public description.',TEXT,'a'*64)]
    assert crawl_once(actor,website)
    path=base+'/sessions/'+session['id']+'/confirm-intake';body={'expected_version':current['version']}
    assert api.post(path,headers=client,json=body).status_code==404
    assert api.post(path,headers=h,json={'expected_version':999}).status_code==409
    confirmed=api.post(path,headers=h,json=body)
    assert confirmed.status_code==200,confirmed.text
    profile=confirmed.json()
    assert profile['approval_status']=='confirmed' and not any(g['critical'] for g in profile['missing_inputs'])
    assert profile['budget_range']['minimum']['amount_paise']==1000000
    assert owner.execute('SELECT count(*) AS n FROM mi.model_dispatches WHERE brand_id=%s',(bid,)).fetchone()['n']==0
    actor=service_factory({'brand':{'id':bid,'organization_id':issued['organization']}},kind='research')
    config=settings();monkeypatch.setattr(config,'research_pilot_brand_id',UUID(bid))
    from app import research_pilot,research_worker
    monkeypatch.setattr(research_pilot,'binding',lambda:(config,actor,config.worker_database_url.get_secret_value()))
    monkeypatch.setattr(research_pilot,'check_model_access',lambda config:{'verified':True})
    provider=RecordedProvider();provider.model=research_pilot.MODEL
    monkeypatch.setattr('app.research_api.get_model_provider',lambda:provider)
    real_run=research_worker.run_once
    monkeypatch.setattr(research_worker,'run_once',lambda actor,**kwargs:real_run(actor,provider=provider,**kwargs))
    path=base+'/sessions/'+session['id']+'/research-pilot'
    assert api.post(path,headers=client,json=body).status_code==404
    response=api.post(path,headers=h,json=body)
    assert response.status_code==200,response.text
    result=response.json();assert result['job']['status']=='succeeded',result
    assert result['session']['research_job_id']==result['job']['id'] and provider.calls==1
    progress=api.get(base+'/sessions/'+session['id']+'/progress',headers=client).json()
    assert progress['research_job']['status']=='succeeded'
    repeat=api.post(path,headers=h,json={'expected_version':result['session']['version']})
    assert repeat.status_code==200 and provider.calls==1
