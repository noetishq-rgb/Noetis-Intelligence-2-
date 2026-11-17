import json
import httpx
import pytest
from app.services.anthropic_provider import AnthropicProvider,ProviderError
from app.services.model_provider import ModelRequest
from app.services.research_sources import BraveDiscovery, collect
from app.services.research_budget import cost_paise
from app.research_models import SourceSeed
from app.services.crawl import CrawlError


def request():
    return ModelRequest(task_type='brand_research',template_version=1,context=[],system='Return sourced research.',
        context_json='{"brand_id":"fixture"}',output_schema_json='{"type":"object","properties":{},"additionalProperties":false}',
        output_schema_name='research-dossier-v1.1',maximum_input_tokens=1000,maximum_output_tokens=512)


def test_anthropic_request_uses_only_output_serialization_and_counts_all_input():
    captured=[]
    def handler(req):
        captured.append(req)
        if req.url.path.endswith('count_tokens'):
            return httpx.Response(200,json={'input_tokens':100})
        return httpx.Response(200,json={'content':[{'type':'tool_use','name':'submit_research_dossier','input':{'fixture':True}}],
            'stop_reason':'tool_use','usage':{'input_tokens':20,'cache_read_input_tokens':80,'output_tokens':10}})
    adapter=AnthropicProvider('fixture-not-live','fixture-model','2023-06-01',httpx.Client(transport=httpx.MockTransport(handler)))
    assert adapter.count_tokens(request())==100
    result=adapter.generate(request())
    assert result.input_tokens==100 and result.output_tokens==10
    payload=json.loads(captured[1].content)
    assert len(payload['tools'])==1 and payload['tools'][0]['name']=='submit_research_dossier'
    assert payload['max_tokens']==512
    assert captured[1].headers['x-api-key']=='fixture-not-live'
    assert captured[1].url.host=='api.anthropic.com'


def test_generation_timeout_is_uncertain_and_never_automatically_retried():
    calls=[]
    def handler(req):
        calls.append(req)
        raise httpx.ReadTimeout('fixture timeout',request=req)
    adapter=AnthropicProvider('fixture-not-live','fixture-model','2023-06-01',httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError) as exc:
        adapter.generate(request())
    assert exc.value.uncertain and not exc.value.retryable
    assert len(calls)==1


def test_count_failures_are_retryable_without_exposing_provider_body():
    adapter=AnthropicProvider('fixture-not-live','fixture-model','2023-06-01',httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(429,json={'secret':'not-for-error-response'}))))
    with pytest.raises(ProviderError) as exc:
        adapter.count_tokens(request())
    assert exc.value.retryable and not exc.value.uncertain
    assert 'not-for-error-response' not in str(exc.value)


def test_brave_discovery_is_india_focused_and_snippets_are_not_evidence():
    requests=[]
    def handler(req):
        requests.append(req)
        return httpx.Response(200,json={'web':{'results':[{'url':'https://example.com/research','description':'An unverified search snippet'},{'url':'file:///etc/passwd'}]}})
    provider=BraveDiscovery('fixture-not-live')
    # Inject a client by patching the constructor used within this adapter.
    from unittest.mock import patch
    client=httpx.Client(transport=httpx.MockTransport(handler))
    with patch('app.services.research_sources.httpx.Client',return_value=client):
        hits=provider.discover('public category India')
    assert len(hits)==1
    assert requests[0].url.params['country']=='IN'
    assert requests[0].url.params['search_lang']=='en'
    assert not hasattr(hits[0],'description')
    def denied(*args,**kwargs):raise CrawlError('robots_denied')
    sources,limitations=collect(hits,[],crawler=denied)
    assert sources==[] and limitations


def test_integer_paise_calculation_does_not_use_floating_rounding():
    assert cost_paise(400,150,1000000,2000000)==700
    assert cost_paise(1,0,1,1)==1
    assert cost_paise(10**12,10**12,10**12,10**12)==2*10**18
    assert cost_paise(1,1,None,1) is None


@pytest.mark.parametrize('payload',[
    {'usage':{'input_tokens':10,'output_tokens':5},'stop_reason':'max_tokens','content':[]},
    {'usage':{'input_tokens':10,'output_tokens':5},'stop_reason':'tool_use','content':[{'type':'tool_use','name':'send_email','input':{}}]},
    {'usage':{'input_tokens':10,'output_tokens':5},'stop_reason':'tool_use','content':[{'type':'tool_use','name':'submit_research_dossier','input':{}}]*2},
    {'usage':{'input_tokens':10,'output_tokens':5},'stop_reason':'tool_use','content':[{'type':'tool_use','name':'submit_research_dossier','input':{}},{'type':'tool_use','name':'send_email','input':{}}]}
])
def test_known_usage_with_truncated_or_unexpected_output_is_retained_for_validation(payload):
    adapter=AnthropicProvider('fixture-not-live','fixture-model','2023-06-01',httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=payload))))
    result=adapter.generate(request())
    assert result.input_tokens==10 and result.output_tokens==5
    assert 'invalid_stop_reason' in json.loads(result.output_json)


@pytest.mark.parametrize('usage',[{}, {'input_tokens':True,'output_tokens':1}, {'input_tokens':1,'output_tokens':-1}])
def test_missing_or_invalid_observed_usage_remains_uncertain(usage):
    adapter=AnthropicProvider('fixture-not-live','fixture-model','2023-06-01',httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'usage':usage}))))
    with pytest.raises(ProviderError) as exc:adapter.generate(request())
    assert exc.value.uncertain and not exc.value.retryable


def test_provider_non_json_response_is_uncertain_without_replay():
    calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(200,content=b'not json')
    adapter=AnthropicProvider('fixture-not-live','fixture-model','2023-06-01',httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError) as exc:adapter.generate(request())
    assert exc.value.uncertain and len(calls)==1


def test_phase4_uses_a_serialization_tool_not_an_action_tool():
    captured=[]
    def handler(req):
        payload=json.loads(req.content);captured.append(payload)
        return httpx.Response(200,json={'usage':{'input_tokens':10,'output_tokens':4},'stop_reason':'tool_use',
            'content':[{'type':'tool_use','name':'submit_workflow_output','input':{'fixture':True}}]})
    adapter=AnthropicProvider('fixture-not-live','fixture-model','2023-06-01',httpx.Client(transport=httpx.MockTransport(handler)))
    planned=request().model_copy(update={'task_type':'strategy','output_schema_name':'strategy-options-v1.1'})
    result=adapter.generate(planned)
    assert captured[0]['tool_choice']['name']=='submit_workflow_output'
    assert [t['name'] for t in captured[0]['tools']]==['submit_workflow_output']
    assert json.loads(result.output_json)=={'fixture':True}
