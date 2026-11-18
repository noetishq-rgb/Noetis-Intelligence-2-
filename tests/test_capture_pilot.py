from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from uuid import UUID,uuid4
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from app import crawl_scheduler
from app.worker import run_once
from tests.test_client_access import issued,login_and_change
from tests.test_api_security import fake_crawl


def test_owner_capture_bridges_empty_client_to_existing_worker_without_inventing_inputs(api,issued,owner,service_factory):
    bid=issued['account']['brand_id'];base='/v1/brands/'+bid+'/brain';h=issued['owner_headers']
    client=login_and_change(api,issued)
    session=api.post(base+'/sessions',headers=client,json={}).json()
    key=str(uuid4());body={'expected_version':session['version'],'website_url':'https://example.com/','max_pages':3}
    capture=api.post(base+'/sessions/'+session['id']+'/capture',headers={**h,'Idempotency-Key':key},json=body)
    assert capture.status_code==202,capture.text
    result=capture.json();assert result['session']['status']=='draft'
    assert result['session']['draft']['category']=='' and result['session']['draft']['objectives']==[]
    assert api.get(base+'/sessions/'+session['id']+'/progress',headers=client).json()['state']=='queued'
    replay=api.post(base+'/sessions/'+session['id']+'/capture',headers={**h,'Idempotency-Key':key},json=body)
    assert replay.status_code==202 and replay.json()['job']['id']==result['job']['id']
    assert api.post(base+'/sessions/'+session['id']+'/capture',headers={**h,'Idempotency-Key':key},json={**body,'website_url':'https://other.example.com/'}).status_code==409
    draft={**result['session']['draft'],'brand_name':'Client edited the brand','category':'Consulting','objectives':['Lead generation']}
    saved=api.put(base+'/sessions/'+session['id'],headers=client,json={'expected_version':result['session']['version'],'draft':draft}).json()
    submitted=api.post(base+'/sessions/'+session['id']+'/submit',headers=client,json={'expected_version':saved['version']})
    assert submitted.status_code==200,submitted.text
    actor=service_factory({'brand':{'id':bid,'organization_id':issued['organization']}})
    assert run_once(actor,fake_crawl)
    profile=owner.execute('SELECT profile FROM mi.brand_profiles WHERE brand_id=%s ORDER BY version DESC LIMIT 1',(bid,)).fetchone()['profile']
    assert profile['name']['value']=='Client edited the brand' and profile['audience_description'] is None and profile['budget_range'] is None
    progress=api.get(base+'/sessions/'+session['id']+'/progress',headers=h).json()
    assert progress['capture_job']['status']=='succeeded' and progress['source_count']>0 and progress['can_synthesise']
    synthesis=api.post(base+'/sessions/'+session['id']+'/synthesise',headers=h,json={'expected_version':submitted.json()['version']})
    assert synthesis.status_code==201,synthesis.text
    assert owner.execute('SELECT count(*) AS n FROM mi.jobs WHERE brand_id=%s',(bid,)).fetchone()['n']==1


def test_capture_is_owner_only_and_rejects_stale_or_unsafe_targets(api,issued,owner):
    client=login_and_change(api,issued);bid=issued['account']['brand_id'];base='/v1/brands/'+bid+'/brain';h=issued['owner_headers']
    s=api.post(base+'/sessions',headers=client,json={}).json();url=base+'/sessions/'+s['id']+'/capture'
    body={'expected_version':s['version'],'website_url':'https://example.com/'}
    assert api.post(url,headers={**client,'Idempotency-Key':str(uuid4())},json=body).status_code==404
    assert api.post(url,headers={**h,'Idempotency-Key':str(uuid4())},json={**body,'expected_version':999}).status_code==409
    assert api.post(url,headers={**h,'Idempotency-Key':str(uuid4())},json={**body,'website_url':'http://localhost/'}).status_code==422
    assert api.post(base+'/capture/run',headers=client).status_code==404
    assert owner.execute('SELECT count(*) AS n FROM mi.jobs WHERE brand_id=%s',(bid,)).fetchone()['n']==0


@pytest.fixture
def scheduler_identity(monkeypatch):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    monkeypatch.setattr(crawl_scheduler,'github_keys',lambda:SimpleNamespace(get_signing_key_from_jwt=lambda token:SimpleNamespace(key=key.public_key())))
    monkeypatch.setattr(crawl_scheduler,'run_bound_capture',lambda:{'processed':False,'job':None})
    now=datetime.now(timezone.utc)
    claims={'iss':crawl_scheduler.ISSUER,'aud':crawl_scheduler.AUDIENCE,'iat':now,'nbf':now,'exp':now+timedelta(minutes=5),
            'sub':'repo:'+crawl_scheduler.REPOSITORY+':ref:refs/heads/main','repository':crawl_scheduler.REPOSITORY,
            'repository_id':'1460555916','ref':'refs/heads/main','workflow_ref':crawl_scheduler.WORKFLOW,'event_name':'schedule'}
    return key,claims


def test_scheduler_requires_real_signature_and_exact_workflow(api,scheduler_identity):
    key,claims=scheduler_identity
    assert api.post('/internal/crawl').status_code==401
    token=jwt.encode(claims,key,algorithm='RS256')
    assert api.post('/internal/crawl',headers={'Authorization':'Bearer '+token}).status_code==200
    badkey=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    forged=jwt.encode(claims,badkey,algorithm='RS256')
    assert api.post('/internal/crawl',headers={'Authorization':'Bearer '+forged}).status_code==401


def test_scheduler_accepts_github_immutable_repository_subject(api,scheduler_identity):
    key,claims=scheduler_identity
    subject='repo:noetishq-rgb@344326924/Noetis-Intelligence-2-@1460555916:ref:refs/heads/main'
    token=jwt.encode({**claims,'sub':subject},key,algorithm='RS256')
    assert api.post('/internal/crawl',headers={'Authorization':'Bearer '+token}).status_code==200
    wrong=jwt.encode({**claims,'sub':subject.replace('@344326924','@123')},key,algorithm='RS256')
    assert api.post('/internal/crawl',headers={'Authorization':'Bearer '+wrong}).status_code==403


@pytest.mark.parametrize('changed',[
    {'aud':'another-project'}, {'repository':'attacker/fork'}, {'repository_id':'1'},
    {'ref':'refs/heads/other'}, {'workflow_ref':'untrusted-workflow'}, {'event_name':'pull_request'},
    {'sub':'repo:attacker/fork:ref:refs/heads/main'}, {'exp':datetime.now(timezone.utc)-timedelta(minutes=2)}])
def test_scheduler_rejects_other_projects_forks_branches_and_expiry(api,scheduler_identity,changed):
    key,claims=scheduler_identity;token=jwt.encode({**claims,**changed},key,algorithm='RS256')
    assert api.post('/internal/crawl',headers={'Authorization':'Bearer '+token}).status_code in {401,403}


def test_hosted_binding_is_selected_by_brand(api, issued, monkeypatch):
    from app.config import settings, CrawlBinding
    config = settings()
    brand = UUID(issued['account']['brand_id'])
    actor = uuid4()
    monkeypatch.setattr(config, 'crawl_scheduler_enabled', True)
    monkeypatch.setattr(config, 'crawl_bindings', {brand: CrawlBinding(service_user_id=actor, database_url='fixture')})
    assert crawl_scheduler.scheduled_brands() == [str(brand)]
    assert crawl_scheduler.capture_binding(brand)[0] == actor
    assert crawl_scheduler.capture_binding(uuid4()) is None
    assert api.get('/internal/crawl/brands').status_code == 401


def test_hosted_queue_declines_unconfigured_brand(api, issued, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings(), 'crawl_scheduler_enabled', True)
    base = '/v1/brands/' + issued['account']['brand_id'] + '/brain'
    h = issued['owner_headers']
    s = api.post(base + '/sessions', headers=h, json={}).json()
    url = base + '/sessions/' + s['id']
    r = api.post(url + '/capture', headers={**h, 'Idempotency-Key': str(uuid4())},
                 json={'expected_version': s['version'], 'website_url': 'https://example.com/'})
    assert r.status_code == 503
    assert api.get(url + '/progress', headers=h).json()['capture_job'] is None
