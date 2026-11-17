from datetime import datetime,timedelta,timezone
from uuid import uuid4
from pydantic import SecretStr
from app.config import settings
from app.auth import digest


def test_founder_activation_is_bound_expiring_and_single_use(api,monkeypatch,owner):
    owner.execute('DELETE FROM identity.auth_attempts');owner.commit()
    config=settings();email=f'activation-{uuid4()}@example.com';token='single-use-test-token-with-enough-entropy-for-fixtures'
    monkeypatch.setattr(config,'allow_signup',False)
    monkeypatch.setattr(config,'bootstrap_email',email)
    monkeypatch.setattr(config,'bootstrap_token_sha256',SecretStr(digest(token)))
    monkeypatch.setattr(config,'bootstrap_expires_at',datetime.now(timezone.utc)+timedelta(minutes=20))
    body={'email':email,'full_name':'Test Founder Activation','password':'test-activation-password-123','token':token}
    assert api.post('/v1/auth/activate-founder',json={**body,'token':'incorrect-token-with-more-than-32-characters'}).status_code==400
    assert api.post('/v1/auth/activate-founder',json={**body,'email':'wrong-owner@example.com'}).status_code==400
    response=api.post('/v1/auth/activate-founder',json=body);assert response.status_code==201,response.text
    headers={'Authorization':'Bearer '+response.json()['access_token']}
    assert api.get('/v1/me',headers=headers).json()['email']==email
    orgs=api.get('/v1/organizations',headers=headers).json();assert len(orgs)==1;assert orgs[0]['role']=='founder'
    assert api.post('/v1/auth/activate-founder',json=body).status_code==409
    assert api.post('/v1/auth/token',json={'email':email,'password':body['password']}).status_code==200
    assert api.post('/v1/auth/register',json={'email':'other@example.com','password':body['password'],'full_name':'Other','organization_name':'Other'}).status_code==404


def test_expired_and_unconfigured_activation_cannot_create_account(api,monkeypatch,owner):
    owner.execute('DELETE FROM identity.auth_attempts');owner.commit()
    c=settings();token='another-test-only-activation-token-123456';email=f'expired-{uuid4()}@example.com'
    body={'email':email,'full_name':'Expired','password':'test-activation-password-123','token':token}
    monkeypatch.setattr(c,'bootstrap_email',email);monkeypatch.setattr(c,'bootstrap_token_sha256',SecretStr(digest(token)));monkeypatch.setattr(c,'bootstrap_expires_at',datetime.now(timezone.utc)-timedelta(seconds=1))
    assert api.post('/v1/auth/activate-founder',json=body).status_code==400
    monkeypatch.setattr(c,'bootstrap_token_sha256',None)
    assert api.post('/v1/auth/activate-founder',json=body).status_code==404
