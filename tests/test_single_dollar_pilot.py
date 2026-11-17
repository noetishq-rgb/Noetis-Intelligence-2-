from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from app import research_pilot as pilot
from app.config import settings
from app.services.research_budget import BudgetError
from tests.test_research import research, RecordedProvider, queue, fake_collect, run_view
from tests.test_client_access import issued
from tests.test_capture_pilot import scheduler_identity


def test_dollar_calculation_and_first_request_ceiling():
    assert pilot.dollars(16000, 4096) == Decimal('0.10944')
    assert pilot.dollars(17600, 4096) < Decimal('1')


@pytest.mark.parametrize('prior,model,maximum_input,maximum_output,error', [
    (1, pilot.MODEL, 16000, 4096, 'pilot_generation_already_attempted'),
    (0, 'another-model', 16000, 4096, 'pilot_model_or_token_limits_changed'),
    (0, pilot.MODEL, 16001, 4096, 'pilot_model_or_token_limits_changed'),
    (0, pilot.MODEL, 16000, 4097, 'pilot_model_or_token_limits_changed'),
])
def test_admission_fails_closed(prior,model,maximum_input,maximum_output,error):
    c=SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: {'n':prior}))
    request=SimpleNamespace(maximum_input_tokens=maximum_input, maximum_output_tokens=maximum_output)
    with pytest.raises(BudgetError,match=error):
        pilot.admit_pilot(c,{'brand_id':uuid4()},request,17600,model)


def test_first_pilot_admission_allowed():
    c=SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: {'n':0}))
    request=SimpleNamespace(maximum_input_tokens=16000, maximum_output_tokens=4096)
    pilot.admit_pilot(c,{'brand_id':uuid4()},request,17600,pilot.MODEL)


def test_dollar_limit_rejects_excessive_reservation():
    request=SimpleNamespace(maximum_input_tokens=16000,maximum_output_tokens=4096)
    with pytest.raises(BudgetError,match='pilot_dollar_budget_exceeded'):
        pilot.admit_pilot(None,{'brand_id':uuid4()},request,1000000,pilot.MODEL)


def test_saved_evidence_uses_explicit_regional_text_not_domain():
    from datetime import datetime,timezone
    page={'url':'https://example.in/','title':'Public page','text_excerpt':'Navigation '*200+'Shipping in India is available.',
          'content_sha256':'a'*64,'accessed_at':datetime.now(timezone.utc)}
    seed,source=pilot.saved_source(page)
    assert seed.geographic_scope=='india' and 'Shipping in India' in source.text[:2400]
    assert source.text in page['text_excerpt'] and source.accessed_at==page['accessed_at']
    seed,_=pilot.saved_source({**page,'text_excerpt':'No location is stated.'})
    assert seed.geographic_scope=='unknown'


@pytest.mark.parametrize('status',[200,401,429])
def test_model_metadata_check_never_exposes_key_or_response_body(monkeypatch,status):
    import httpx
    config=SimpleNamespace(anthropic_api_key=SecretStr('fixture-secret-key'),anthropic_model=pilot.MODEL,anthropic_version='2026-10-21')
    class Client:
        def __init__(self,*args,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,url,headers):
            assert headers['x-api-key']=='fixture-secret-key' and url.endswith('/'+pilot.MODEL)
            return httpx.Response(status,json={'id':pilot.MODEL,'private':'must-not-return'})
    monkeypatch.setattr(pilot.httpx,'Client',Client)
    result=pilot.check_model_access(config)
    assert result['verified']==(status==200)
    assert 'fixture-secret-key' not in repr(result) and 'must-not-return' not in repr(result)


def test_invalid_output_does_not_admit_a_second_paid_generation(api,research,monkeypatch,owner):
    from app.research_worker import run_once
    monkeypatch.setattr(settings(),'research_pilot_brand_id',UUID(research['bid']))
    provider=RecordedProvider(mode='bad_quote');provider.model=pilot.MODEL
    job=queue(api,research)
    assert run_once(research['actor'],provider=provider,collector=fake_collect)
    assert provider.calls==1
    owner.execute("SELECT set_config('app.actor_id',%s,true)",(str(research['actor']),))
    owner.execute("UPDATE mi.jobs SET available_at=now() WHERE id=%s",(job['job_id'],));owner.commit()
    assert run_once(research['actor'],provider=provider,collector=fake_collect)
    assert provider.calls==1
    state=run_view(api,research,job)
    assert state['last_error']=='pilot_generation_already_attempted'


def test_pilot_reuses_captured_timestamp_and_never_runs_twice(api,research,monkeypatch,owner):
    import tests.test_research as fixtures
    captured=owner.execute('SELECT text_excerpt FROM mi.crawl_pages WHERE brand_id=%s LIMIT 1',(research['bid'],)).fetchone()
    monkeypatch.setattr(fixtures,'TEXT',captured['text_excerpt'])
    from app.services.research_storage import ensure_template
    owner.execute("SELECT set_config('app.actor_id',%s,true)",(str(research['actor']),))
    brand=owner.execute('SELECT * FROM mi.brand_workspaces WHERE id=%s',(research['bid'],)).fetchone()
    ensure_template(owner,brand,research['actor'],'brand_research')
    owner.commit()
    config=settings()
    monkeypatch.setattr(config,'research_pilot_brand_id',UUID(research['bid']))
    monkeypatch.setattr(pilot,'binding',lambda:(config,research['actor'],config.worker_database_url.get_secret_value()))
    monkeypatch.setattr(pilot,'check_model_access',lambda config:{'verified':True})
    provider=RecordedProvider();provider.model=pilot.MODEL
    from app import research_worker
    real_run=research_worker.run_once
    monkeypatch.setattr(research_worker,'run_once',lambda actor,**kwargs:real_run(actor,provider=provider,**kwargs))
    result=pilot.run_pilot('run')
    import json
    # The fake public capture has no India evidence; retain validation failure
    # rather than inventing geography to make a pilot pass.
    assert result['job']['status']=='failed',json.dumps(result,default=str)
    assert result['job']['last_error']=='research_output_validation_failed'
    assert result['generation_attempts']==1 and provider.calls==1
    assert result['input_tokens']>0 and result['recorded_token_cost_usd']!='0'
    assert not pilot.run_pilot('run')['processed'] and provider.calls==1
    captured=owner.execute('SELECT min(created_at) AS t FROM mi.crawl_pages WHERE brand_id=%s',(research['bid'],)).fetchone()['t']
    cited=owner.execute("SELECT min(s.accessed_at) AS t FROM mi.research_evidence e JOIN mi.research_sources s ON s.id=e.source_id WHERE e.brand_id=%s",(research['bid'],)).fetchone()['t']
    assert cited<=captured


def test_missing_intake_never_creates_research_job(api,issued,owner,monkeypatch):
    from tests.test_client_access import login_and_change
    client=login_and_change(api,issued)
    bid=UUID(issued['account']['brand_id'])
    api.post(f'/v1/brands/{bid}/brain/sessions',headers=client,json={})
    with owner.transaction():
        state=pilot.readiness(owner,bid)
    assert not state['ready'] and not state['profile_confirmed']


def test_scheduled_events_cannot_start_pilot():
    with pytest.raises(HTTPException) as error:
        pilot.pilot(pilot.PilotAction(action='run'),{'event_name':'schedule'})
    assert error.value.status_code==403


def test_pilot_endpoint_requires_signed_explicit_dispatch(api,scheduler_identity,monkeypatch):
    import jwt
    key,claims=scheduler_identity
    monkeypatch.setattr(pilot,'run_pilot',lambda action:{'processed':False,'action':action})
    assert api.post('/internal/research-pilot',json={'action':'run'}).status_code==401
    for event in ('schedule','push','workflow_dispatch'):
        token=jwt.encode({**claims,'event_name':event},key,algorithm='RS256')
        response=api.post('/internal/research-pilot',json={'action':'run'},headers={'Authorization':'Bearer '+token})
        assert response.status_code==(200 if event=='workflow_dispatch' else 403)
