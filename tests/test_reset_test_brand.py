from uuid import UUID,uuid4
import pytest
import psycopg
from tests.test_client_access import issued,login_and_change
from tests.test_api_security import fake_crawl
from app.worker import run_once
from scripts.reset_test_brand import reset_sql


def test_scoped_test_reset_erases_inputs_and_sources_preserves_access_and_audit(api,issued,owner,service_factory,brand_factory,agency):
    client=login_and_change(api,issued);bid=issued['account']['brand_id'];base=f'/v1/brands/{bid}/brain';h=issued['owner_headers']
    other=brand_factory(agency,'Unrelated brand')['brand']
    session=api.post(base+'/sessions',headers=client,json={'brand_name':'Temporary brand','category':'Testing','objectives':['Brand awareness'],'online_presence':[{'url':'https://example.com/','platform':'website'}]}).json()
    capture=api.post(base+'/sessions/'+session['id']+'/capture',headers={**h,'Idempotency-Key':str(uuid4())},json={'expected_version':session['version']})
    assert capture.status_code==202,capture.text
    session=capture.json()['session']
    actor=service_factory({'brand':{'id':bid,'organization_id':issued['organization']}})
    assert run_once(actor,fake_crawl)
    submitted=api.post(base+'/sessions/'+session['id']+'/submit',headers=client,json={'expected_version':session['version']}).json()
    assert api.post(base+'/sessions/'+session['id']+'/synthesise',headers=h,json={'expected_version':submitted['version']}).status_code==201
    founder=owner.execute("SELECT user_id FROM mi.organization_memberships WHERE organization_id=%s AND role='founder'",(issued['organization'],)).fetchone()['user_id']
    label=owner.execute('SELECT name FROM mi.brand_workspaces WHERE id=%s',(bid,)).fetchone()['name']
    before=owner.execute('SELECT count(*) AS n FROM mi.audit_logs WHERE brand_id=%s',(bid,)).fetchone()['n'];owner.commit()
    owner.execute(reset_sql(UUID(bid),founder,label));owner.commit()
    for table in ['crawl_pages','brand_profiles','brand_onboarding_sessions','brand_brain_versions','research_sources','jobs','artifacts','workflow_events','idempotency_keys']:
        assert owner.execute(f'SELECT count(*) AS n FROM mi.{table} WHERE brand_id=%s',(bid,)).fetchone()['n']==0,table
    assert owner.execute('SELECT state FROM mi.workflow_instances WHERE brand_id=%s',(bid,)).fetchone()['state']=='brand_created'
    assert owner.execute('SELECT count(*) AS n FROM mi.audit_logs WHERE brand_id=%s',(bid,)).fetchone()['n']>before
    assert owner.execute('SELECT count(*) AS n FROM mi.client_accounts WHERE brand_id=%s',(bid,)).fetchone()['n']==1
    assert owner.execute('SELECT name FROM mi.brand_workspaces WHERE id=%s',(other['id'],)).fetchone()['name']=='Unrelated brand'
    assert api.get('/v1/me',headers=client).status_code==200
    assert api.get(base+'/sessions/current',headers=client).status_code==404
    fresh=api.post(base+'/sessions',headers=client,json={'research_intake_version':1}).json()
    assert fresh['draft']['brand_name']=='' and fresh['crawl_job_id'] is None and fresh['draft']['budget_range'] is None
    assert owner.execute("SELECT count(*) AS n FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid WHERE p.proname='reject_mutation' AND t.tgenabled='D'").fetchone()['n']==0


def test_reset_refuses_wrong_workspace_name_before_deleting(api,issued,owner):
    bid=UUID(issued['account']['brand_id'])
    founder=owner.execute("SELECT user_id FROM mi.organization_memberships WHERE organization_id=%s AND role='founder'",(issued['organization'],)).fetchone()['user_id'];owner.commit()
    with pytest.raises(psycopg.errors.RaiseException,match='exact test workspace'):
        with owner.transaction():owner.execute(reset_sql(bid,founder,'incorrect brand'))
    assert owner.execute('SELECT id FROM mi.brand_workspaces WHERE id=%s',(bid,)).fetchone()
