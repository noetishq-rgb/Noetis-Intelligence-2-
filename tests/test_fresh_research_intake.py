from uuid import UUID,uuid4
import pytest
from app.config import settings
from app.services.crawl import CrawledPage
from app.worker import run_once as crawl_once
from tests.test_client_access import issued,login_and_change
from tests.test_research import RecordedProvider,TEXT


def rich_draft():
    return {'research_intake_version':1,'brief':{'question':'Which benefits are documented?', 'tone_of_voice':'Direct', 'guidelines':'Use evidence.'},'brand_name':'Fresh client brand','category':'Food & beverage',
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
    # Simulate an older capture which did not install any prompt templates.
    with monkeypatch.context() as older_capture:
        older_capture.setattr('app.services.research_storage.ensure_template', lambda *args: None)
        capture=api.post(base+'/sessions/'+session['id']+'/capture',headers={**h,'Idempotency-Key':str(uuid4())},json={'expected_version':submitted.json()['version']})
    assert capture.status_code==202,capture.text
    assert owner.execute('SELECT count(*) AS n FROM mi.prompt_templates WHERE brand_id=%s',(bid,)).fetchone()['n']==0
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
    question='Which messages have evidence on our site?'
    response=api.post(path,headers=h,json={**body,'question':question})
    assert response.status_code==200,response.text
    result=response.json();assert result['job']['status']=='succeeded',result
    import json
    assert json.loads(provider.requests[0].context_json)['brand_brief'] == {**rich_draft()['brief'],'question':question}
    owner_progress=api.get(base+'/sessions/'+session['id']+'/progress',headers=h).json()
    assert owner_progress['research_question']==question
    assert result['session']['research_job_id']==result['job']['id'] and provider.calls==1
    progress=api.get(base+'/sessions/'+session['id']+'/progress',headers=client).json()
    assert progress['research_job']['status']=='succeeded'
    repeat=api.post(path,headers=h,json={'expected_version':result['session']['version']})
    assert repeat.status_code==200 and provider.calls==1


def test_client_can_complete_legacy_intake_after_capture(api, issued, service_factory):
    from tests.test_api_security import fake_crawl
    client = login_and_change(api, issued)
    h = issued['owner_headers']
    bid = issued['account']['brand_id']
    base = f'/v1/brands/{bid}/brain/sessions'
    rich = rich_draft()
    legacy = {k: rich[k] for k in ['brand_name','category','objectives','online_presence']}
    old = api.post(base, headers=client, json=legacy).json()
    url = base + '/' + old['id']
    submitted = api.post(url + '/submit', headers=client, json={'expected_version': old['version']}).json()
    cap = api.post(url + '/capture', headers={**h,'Idempotency-Key': str(uuid4())},
                   json={'expected_version': submitted['version']}).json()
    actor = service_factory({'brand': {'id': bid, 'organization_id': issued['organization']}})
    assert crawl_once(actor, fake_crawl)
    rev = {'expected_version': cap['session']['version']}
    r = api.post(url + '/complete-intake', headers=client, json=rev)
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft['id'] != old['id'] and draft['crawl_job_id'] == cap['job']['id']
    assert draft['draft']['objectives'] == legacy['objectives']
    assert api.post(url + '/complete-intake', headers=client, json=rev).status_code == 409
    url = base + '/' + draft['id']
    saved = api.put(url, headers=client, json={'expected_version': draft['version'], 'draft': rich}).json()
    sent = api.post(url + '/submit', headers=client, json={'expected_version': saved['version']}).json()
    confirmed = api.post(url + '/confirm-intake', headers=h, json={'expected_version': sent['version']})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()['approval_status'] == 'confirmed'
