from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.session_cookie import COOKIE
from app.db import transaction
import psycopg


@pytest.fixture
def issued(api,agency,owner):
    owner.execute('DELETE FROM identity.auth_attempts');owner.commit()
    body={'brand_name':'Client-owned brand','email':f'client-{uuid4()}@example.com','full_name':'Brand contact'}
    response=api.post(f"/v1/organizations/{agency['organization']}/clients",headers=agency['headers'],json=body)
    assert response.status_code==201,response.text
    return {**response.json(),'organization':agency['organization'],'owner_headers':agency['headers']}


def login_and_change(api,issued):
    login=api.post('/v1/auth/token',json={'email':issued['account']['email'],'password':issued['temporary_password']})
    assert login.status_code==200,login.text
    headers={'Authorization':'Bearer '+login.json()['access_token']}
    assert api.get('/v1/brands',headers=headers).status_code==403
    me=api.get('/v1/me',headers=headers).json();assert me['requires_password_change'] and me['managed_client']
    response=api.post('/v1/auth/password',headers=headers,json={'current_password':issued['temporary_password'],'new_password':'Client-chosen-private-password-123'})
    assert response.status_code==200,response.text
    assert api.get('/v1/me',headers=headers).status_code==401
    return {'Authorization':'Bearer '+response.json()['access_token']}


def test_owner_issues_empty_workspace_client_completes_and_returns(api,issued,owner):
    bid=issued['account']['brand_id'];base='/v1/brands/'+bid+'/brain'
    assert owner.execute('SELECT count(*) AS n FROM mi.jobs WHERE brand_id=%s',(bid,)).fetchone()['n']==0
    assert owner.execute('SELECT website_url FROM mi.brand_workspaces WHERE id=%s',(bid,)).fetchone()['website_url'] is None
    headers=login_and_change(api,issued)
    assert [b['id'] for b in api.get('/v1/brands',headers=headers).json()]==[bid]
    draft={'brand_name':'Client entered this name','category':'Consulting','online_presence':[{'url':'https://example.com','platform':'website'}],'objectives':['Lead generation']}
    created=api.post(base+'/sessions',headers=headers,json={'brand_name':'Partial client input'})
    assert created.status_code==201,created.text
    session=created.json()
    saved=api.put(base+'/sessions/'+session['id'],headers=headers,json={'expected_version':session['version'],'draft':draft})
    assert saved.status_code==200,saved.text
    assert api.get(base+'/sessions/current',headers=headers).json()['draft']['category']=='Consulting'
    submitted=api.post(base+'/sessions/'+session['id']+'/submit',headers=headers,json={'expected_version':saved.json()['version']})
    assert submitted.status_code==200,submitted.text
    progress=api.get(base+'/sessions/'+session['id']+'/progress',headers=headers).json()
    assert progress['state']=='awaiting_configuration' and progress['capture_job'] is None
    assert progress['source_count']==0 and not progress['can_synthesise']
    assert api.get('/v1/brands/'+bid,headers=headers).json()['name']==draft['brand_name']
    directory=api.get(f"/v1/organizations/{issued['organization']}/clients",headers=issued['owner_headers']).json()
    assert directory[0]['onboarding_status']=='submitted'
    assert 'password_hash' not in str(directory) and issued['temporary_password'] not in str(directory)


def test_cross_tenant_and_client_account_management_are_denied(api,issued,agency,owner):
    headers=login_and_change(api,issued)
    path=f"/v1/organizations/{issued['organization']}/clients"
    assert api.get(path,headers=headers).status_code==404
    assert api.post(path,headers=headers,json={'brand_name':'Hacked','email':'hack@example.com','full_name':'Hack'}).status_code==404
    other=api.post('/v1/auth/register',json={'email':f'other-{uuid4()}@example.com','full_name':'Other','organization_name':'Other','password':'Other-account-password-123'}).json()
    oh={'Authorization':'Bearer '+other['access_token']}
    assert api.get(path,headers=oh).status_code==404
    assert api.get('/v1/brands/'+issued['account']['brand_id']+'/brain/sessions/current',headers=oh).status_code==404
    own_org=api.get('/v1/organizations',headers=oh).json()[0]['id']
    assert api.post(f"/v1/organizations/{own_org}/clients/{issued['account']['id']}/access",headers=oh,json={'action':'suspend'}).status_code==404
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with transaction(api.get('/v1/me',headers=headers).json()['id']) as c:
            c.execute('SELECT identity.create_client_user(%s,%s,%s,%s,%s)',(issued['organization'],uuid4(),'unauthorized@example.com','Bad','$argon2id$fake'))


def test_suspend_and_reset_revoke_existing_sessions_without_deleting_data(api,issued):
    headers=login_and_change(api,issued)
    path=f"/v1/organizations/{issued['organization']}/clients/{issued['account']['id']}/access"
    assert api.post(path,headers=issued['owner_headers'],json={'action':'suspend'}).status_code==200
    assert api.get('/v1/me',headers=headers).status_code==401
    assert api.post('/v1/auth/token',json={'email':issued['account']['email'],'password':'Client-chosen-private-password-123'}).status_code==401
    assert api.post(path,headers=issued['owner_headers'],json={'action':'resume'}).status_code==200
    login=api.post('/v1/auth/token',json={'email':issued['account']['email'],'password':'Client-chosen-private-password-123'}).json()
    reset=api.post(path,headers=issued['owner_headers'],json={'action':'reset_password'}).json()
    assert reset['temporary_password'] and reset['account']['requires_password_change']
    assert api.get('/v1/me',headers={'Authorization':'Bearer '+login['access_token']}).status_code==401
    assert api.get('/v1/brands/'+issued['account']['brand_id'],headers=issued['owner_headers']).status_code==200


def test_https_cookie_restores_rotates_and_logout_revokes(api,agency,owner):
    owner.execute('DELETE FROM identity.auth_attempts');owner.commit()
    with TestClient(app,base_url='https://testserver') as browser:
        login=browser.post('/v1/auth/token',json={'email':agency['email'],'password':agency['password']})
        cookie=login.headers['set-cookie'];assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=lax' in cookie
        first=browser.cookies.get(COOKIE)
        assert browser.post('/v1/auth/session').status_code==403
        assert browser.post('/v1/auth/session',headers={'X-NOETIS-Session':'1','Origin':'https://evil.example'}).status_code==403
        restored=browser.post('/v1/auth/session',headers={'X-NOETIS-Session':'1','Origin':'https://testserver'})
        assert restored.status_code==200,restored.text
        assert browser.cookies.get(COOKIE)!=first
        h={'Authorization':'Bearer '+restored.json()['access_token']}
        assert browser.get('/v1/me',headers=h).status_code==200
        assert browser.post('/v1/auth/logout',headers=h).status_code==200
        assert browser.post('/v1/auth/session',headers={'X-NOETIS-Session':'1'}).status_code==401


def test_client_cannot_reuse_existing_identity_or_link_another_brand(api,issued):
    headers=login_and_change(api,issued)
    duplicate=api.post(f"/v1/organizations/{issued['organization']}/clients",headers=issued['owner_headers'],json={'brand_name':'Duplicate','email':issued['account']['email'],'full_name':'Duplicate'})
    assert duplicate.status_code==409
    assert api.post('/v1/me/accept-invitation',headers=headers,json={'invitation_token':'unused-invitation-token-that-is-long-enough'}).status_code==403
