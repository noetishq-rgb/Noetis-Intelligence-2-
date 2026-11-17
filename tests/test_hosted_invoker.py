import io,json
from urllib.error import HTTPError
import pytest
from scripts import run_hosted_crawl as invoker


def test_invoker_waits_for_the_new_deployment_without_printing_identity_token(monkeypatch,capsys):
    monkeypatch.setenv('GITHUB_SHA','new-revision')
    monkeypatch.setenv('ACTIONS_ID_TOKEN_REQUEST_URL','https://token.actions.githubusercontent.com/fixture')
    monkeypatch.setenv('ACTIONS_ID_TOKEN_REQUEST_TOKEN','fixture-request-token')
    calls=[];pauses=[]
    def open_request(request,timeout):
        calls.append(request)
        if request.full_url.endswith('/health'):
            response=io.BytesIO(b'{}');response.headers={'X-Noetis-Revision':'old' if len(calls)==1 else 'new-revision'};return response
        if len(calls)==3:return io.BytesIO(json.dumps({'value':'fixture-signed-token'}).encode())
        if len(calls)==4:raise HTTPError(invoker.ENDPOINT,405,'Previous deployment',{},None)
        return io.BytesIO(json.dumps({'processed':True,'job':{'status':'succeeded'}}).encode())
    monkeypatch.setattr(invoker,'urlopen',open_request);monkeypatch.setattr(invoker.time,'sleep',pauses.append)
    invoker.main();printed=capsys.readouterr().out
    assert json.loads(printed)['job_status']=='succeeded' and pauses==[5,5]
    assert 'fixture-signed-token' not in printed and 'fixture-request-token' not in printed
    assert len(calls)==5


def test_invoker_never_retries_an_authorization_failure(monkeypatch):
    monkeypatch.delenv('GITHUB_SHA',raising=False)
    monkeypatch.setenv('ACTIONS_ID_TOKEN_REQUEST_URL','https://token.actions.githubusercontent.com/fixture')
    monkeypatch.setenv('ACTIONS_ID_TOKEN_REQUEST_TOKEN','fixture-request-token')
    calls=[]
    def open_request(request,timeout):
        calls.append(request)
        if len(calls)==1:return io.BytesIO(b'{"value":"fixture-signed-token"}')
        raise HTTPError(invoker.ENDPOINT,403,'Denied',{},None)
    monkeypatch.setattr(invoker,'urlopen',open_request)
    with pytest.raises(SystemExit,match='403'):invoker.main()
    assert len(calls)==2
