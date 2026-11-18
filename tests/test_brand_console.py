from types import SimpleNamespace
import httpx
from pydantic import SecretStr
from app import console_api
from app.config import settings
from tests.test_client_access import issued, login_and_change


def test_assigned_client_chat_budget_and_activation(api, issued, monkeypatch, owner):
    client = login_and_change(api, issued)
    path = '/v1/brands/' + issued['account']['brand_id'] + '/console/ask'
    body = {'question': 'What is in the brief?', 'documents': [{'name':'brief.txt','text':'Use plain language.'}]}
    assert api.post(path, json=body).status_code == 401
    assert api.post(path, headers=client, json=body).status_code == 503
    cfg = settings()
    monkeypatch.setattr(cfg,'console_enabled',True)
    monkeypatch.setattr(cfg,'anthropic_model',console_api.MODEL)
    monkeypatch.setattr(cfg,'anthropic_api_key',SecretStr('fixture'))
    calls = []
    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(200,json={'input_tokens':123} if request.url.path.endswith('count_tokens') else
                              {'content':[{'type':'text','text':'Use plain language (brief.txt).'}]})
    real_client = httpx.Client
    monkeypatch.setattr(console_api,'httpx',SimpleNamespace(Client=lambda **kw: real_client(transport=httpx.MockTransport(handle),**kw),HTTPError=httpx.HTTPError))
    result=api.post(path,headers=client,json=body)
    assert result.status_code==200 and 'brief.txt' in result.json()['reply']
    owner.execute("SELECT setval('mi.console_trial_slots',75,true)")
    assert api.post(path,headers=client,json=body).status_code==429
    assert calls.count('/v1/messages')==1
