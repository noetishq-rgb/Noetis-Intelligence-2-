from uuid import uuid4,UUID
import pytest
from app.worker import run_once
from tests.test_api_security import fake_crawl

SETUP={'brand_name':'Example research brand','online_presence':[{'url':'https://example.com','platform':'website'},{'url':'https://www.instagram.com/example','platform':'instagram'}],'category':'Consulting','niche':'Independent experts','objectives':['Lead generation'],'other_objective':''}

@pytest.fixture
def brain(api,agency,brand_factory,service_factory):
    b=brand_factory(agency);bid=b['brand']['id'];base=f'/v1/brands/{bid}/brain';h=agency['headers']
    s=api.post(base+'/sessions',headers=h,json=SETUP);assert s.status_code==201,s.text;s=s.json()
    return {'brand':b,'bid':bid,'base':base,'headers':h,'session':s,'actor':service_factory(b)}


def captured(api,brain):
    base,h=brain['base'],brain['headers'];s=brain['session']
    r=api.post(base+f"/sessions/{s['id']}/submit",headers=h,json={'expected_version':s['version']});assert r.status_code==200,r.text;s=r.json()
    assert run_once(brain['actor'],fake_crawl)
    r=api.post(base+f"/sessions/{s['id']}/synthesise",headers=h,json={'expected_version':s['version']});assert r.status_code==201,r.text
    return r.json()


def test_real_job_draft_resume_sources_and_truthful_stages(api,brain):
    h,base,s=brain['headers'],brain['base'],brain['session']
    assert api.get(base+'/sessions/current',headers=h).json()['id']==s['id']
    r=api.put(base+f"/sessions/{s['id']}",headers=h,json={'expected_version':999,'draft':SETUP});assert r.status_code==409
    p=api.get(base+f"/sessions/{s['id']}/progress",headers=h).json();assert p['state']=='created';assert p['source_count']==0;assert all(s['state']=='pending' for s in p['stages'])
    v=captured(api,brain);assert len(v['document']['chapters'])==6
    p=api.get(base+f"/sessions/{s['id']}/progress",headers=h).json();assert p['state']=='awaiting_input';assert p['source_count']>0
    assert p['stages'][3]['state']=='needs_review';assert p['stages'][5]['state']=='completed'
    assert len(api.get(base+'/versions',headers=h).json())==1
    r=api.post(base+f"/sessions/{s['id']}/synthesise",headers=h,json={'expected_version':2});assert r.status_code==201;assert r.json()['id']==v['id']


def test_review_originals_comments_conflicts_partial_confirmation_and_restore(api,brain):
    v=captured(api,brain);base,h=brain['base'],brain['headers'];vid=v['id'];ch=next(c for c in v['document']['chapters'] if c['key']=='objectives');f=ch['findings'][0]
    review=base+f"/versions/{vid}/findings/{f['id']}/review"
    r=api.post(review,headers=h,json={'expected_sequence':0,'action':'edit','text':'Generate qualified consulting enquiries','reason':'Founder correction'});assert r.status_code==201,r.text
    updated=next(c for c in r.json()['document']['chapters'] if c['key']=='objectives')['findings'][0];assert updated['original_text']==f['text'];assert updated['text']!=f['text'];assert updated['history'][0]['actor_name']=='Test Founder'
    assert api.post(review,headers=h,json={'expected_sequence':0,'action':'accept','reason':'Review'}).status_code==409
    r=api.post(review,headers=h,json={'expected_sequence':1,'action':'accept','reason':'Founder confirms corrected objective'});assert r.status_code==201,r.text
    t=api.post(base+f'/versions/{vid}/threads',headers=h,json={'chapter':'objectives','finding_id':f['id'],'body':'Please confirm the definition'});assert t.status_code==201,t.text;tid=t.json()['id']
    assert api.post(base+f'/threads/{tid}/comments',headers=h,json={'body':'Confirmed by the founder'}).status_code==201
    body={'expected_review_sequence':2,'approved_chapters':['objectives'],'partial':True,'acknowledge_limitations':True}
    assert api.post(base+f'/versions/{vid}/confirm',headers=h,json=body).status_code==409
    assert api.post(base+f'/threads/{tid}/state',headers=h,json={'action':'resolve','expected_event':None}).status_code==201
    r=api.post(base+f'/versions/{vid}/confirm',headers=h,json=body);assert r.status_code==201,r.text;assert r.json()['status']=='partial';assert r.json()['actor_name']=='Test Founder';assert len(r.json()['snapshot']['chapters'])==1
    assert api.post(review,headers=h,json={'expected_sequence':2,'action':'edit','text':'Change','reason':'New edit'}).status_code==409
    before=api.get(base+'/current',headers=h).json()
    fork=api.post(base+f'/versions/{vid}/fork',headers=h);assert fork.status_code==201,fork.text;assert fork.json()['version']==2
    assert api.get(base+'/current',headers=h).json()['id']==before['id']
    assert api.get(base+f'/versions/{vid}',headers=h).json()['document']['chapters']==r.json()['snapshot']['chapters'] or len(api.get(base+f'/versions/{vid}',headers=h).json()['document']['chapters'])==6


def test_unknown_findings_cannot_be_accepted_and_incomplete_versions_cannot_be_fully_confirmed(api,brain):
    v=captured(api,brain);base,h=brain['base'],brain['headers'];vid=v['id'];f=next(f for c in v['document']['chapters'] for f in c['findings'] if f['unknown'])
    r=api.post(base+f"/versions/{vid}/findings/{f['id']}/review",headers=h,json={'expected_sequence':0,'action':'accept','reason':'Accept unknown'});assert r.status_code==409
    r=api.post(base+f'/versions/{vid}/confirm',headers=h,json={'expected_review_sequence':0,'approved_chapters':['market'],'partial':False,'acknowledge_limitations':True});assert r.status_code==409
    assert api.get(base+'/current',headers=h).status_code==404


def test_cross_tenant_nested_ids_and_unauthenticated_access_fail(api,agency,brain,brand_factory):
    v=captured(api,brain);other=brand_factory(agency);base=f"/v1/brands/{other['brand']['id']}/brain";h=brain['headers'];s=brain['session']
    assert api.get(brain['base']+'/overview').status_code==401
    assert api.get(base+f"/versions/{v['id']}",headers=h).status_code==404
    assert api.get(base+f"/sessions/{s['id']}/progress",headers=h).status_code==404
    assert api.post(base+f"/versions/{v['id']}/fork",headers=h).status_code==404


def test_social_only_workspace_never_invents_a_website(api,agency,service_factory):
    r=api.post(f"/v1/organizations/{agency['organization']}/brands",headers={**agency['headers'],'Idempotency-Key':str(uuid4())},json={'name':'Social-first brand','online_presence':[{'url':'https://www.instagram.com/example','platform':'instagram'}],'category':'Services','objectives':['Brand awareness']})
    assert r.status_code==201,r.text;b=r.json();assert b['brand']['website_url'] is None
    p=api.get(f"/v1/brands/{b['brand']['id']}/profile",headers=agency['headers']);assert p.status_code==200,p.text;assert p.json()['website_url'] is None;assert p.json()['schema_version']=='1.1'
    actor=service_factory(b);assert run_once(actor,fake_crawl)


def test_full_confirmation_promotes_only_through_existing_canonical_gates(api,brain):
    v=captured(api,brain);base,h=brain['base'],brain['headers'];vid=v['id'];review_count=0
    for ch in v['document']['chapters']:
        for f in ch['findings']:
            sequence=0
            if f['unknown']:
                r=api.post(base+f"/versions/{vid}/findings/{f['id']}/review",headers=h,json={'expected_sequence':sequence,'action':'edit','text':'Founder supplied test information for '+ch['title'],'reason':'Controlled fixture founder input'});assert r.status_code==201,r.text;sequence+=1;review_count+=1
            r=api.post(base+f"/versions/{vid}/findings/{f['id']}/review",headers=h,json={'expected_sequence':sequence,'action':'accept','reason':'Controlled fixture human review'});assert r.status_code==201,r.text;review_count+=1
    r=api.post(base+f'/versions/{vid}/confirm',headers=h,json={'expected_review_sequence':review_count,'approved_chapters':['business','audience','positioning','market','presence','objectives'],'partial':False,'acknowledge_limitations':True});assert r.status_code==201,r.text;assert r.json()['status']=='confirmed'
    # A Brain confirmation cannot erase the canonical intake's budget/audience guards.
    assert api.post(base+f'/versions/{vid}/canonical-profile',headers=h,json={}).status_code==409
    r=api.post(base+f'/versions/{vid}/canonical-profile',headers=h,json={'business_description':'Consulting for independent experts','audience_description':'India-based consulting buyers','budget_range':{'minimum':{'currency':'INR','amount_paise':1000000},'maximum':{'currency':'INR','amount_paise':2000000}},'target_geographies':['India']});assert r.status_code==200,r.text;assert r.json()['approval_status']=='confirmed'
    workflow=api.get('/v1/brands/'+brain['bid']+'/workflow',headers=h).json();assert workflow['state']=='ready_for_research'


def test_assigned_clients_collaborate_but_cannot_confirm_or_read_another_brand(api,agency,brain,brand_factory):
    from tests.test_api_security import invite_user
    v=captured(api,brain);base=brain['base'];h=invite_user(api,agency,brain['brand'],'client')
    assert api.get(base+'/overview',headers=h).status_code==200
    p=api.get(base+f"/sessions/{brain['session']['id']}/progress",headers=h);assert p.status_code==200,p.text;assert p.json()['stages'][0]['state']=='completed'
    t=api.post(base+f"/versions/{v['id']}/threads",headers=h,json={'chapter':'business','body':'A client clarification'});assert t.status_code==201,t.text
    activity=api.get(base+'/activity',headers=h);assert activity.status_code==200;assert any(a['actor_name']=='Invited User' for a in activity.json())
    f=v['document']['chapters'][-1]['findings'][0]
    assert api.post(base+f"/versions/{v['id']}/findings/{f['id']}/review",headers=h,json={'expected_sequence':0,'action':'accept','reason':'Client approval'}).status_code==404
    assert api.post(base+f"/versions/{v['id']}/confirm",headers=h,json={'expected_review_sequence':0,'approved_chapters':['objectives'],'partial':True,'acknowledge_limitations':True}).status_code==404
    other=brand_factory(agency);assert api.get(f"/v1/brands/{other['brand']['id']}/brain/overview",headers=h).status_code==404


def test_partial_url_drafts_save_but_cannot_be_submitted(api,brain):
    s=brain['session'];base,h=brain['base'],brain['headers'];draft={**SETUP,'online_presence':[{'url':'https://','platform':'other'}]}
    r=api.put(base+f"/sessions/{s['id']}",headers=h,json={'expected_version':s['version'],'draft':draft});assert r.status_code==200,r.text
    assert api.get(base+'/sessions/current',headers=h).json()['draft']['online_presence'][0]['url']=='https://'
    assert api.post(base+f"/sessions/{s['id']}/submit",headers=h,json={'expected_version':r.json()['version']}).status_code==422
