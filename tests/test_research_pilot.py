import json
from scripts import research_pilot
from app.services.research_sources import CollectedSource
from app.research_models import SourceSeed


def test_preflight_does_not_expose_secrets_or_make_provider_calls(monkeypatch):
    monkeypatch.setattr(research_pilot,'dotenv_values',lambda path:{})
    for key,value in {'MI_ALLOW_MODEL_CALLS':'true','MI_MODEL_PROVIDER':'anthropic',
        'MI_ANTHROPIC_API_KEY':'fixture-secret-not-for-display','MI_ANTHROPIC_MODEL':'fixture-model'}.items():
        monkeypatch.setenv(key,value)
    report=research_pilot.preflight()
    assert report['provider_configuration_ready'] and not report['live_provider_verified']
    assert report['model_calls']==0 and 'fixture-secret-not-for-display' not in json.dumps(report)


def test_capture_failures_remain_explicit_without_a_generated_dossier(tmp_path,monkeypatch):
    monkeypatch.setattr(research_pilot,'collect',lambda seeds,queries,trace:([],['source: fetch_failed']))
    monkeypatch.setattr(research_pilot,'preflight',lambda:{'provider_configuration_ready':False})
    path=tmp_path/'manifest.json'
    path.write_text(json.dumps({'brand_name':'Example','website_url':'https://example.com','purpose':'Public research',
        'sources':[{'url':'https://example.com'}],'limitations':[]}))
    result=research_pilot.capture(path,tmp_path/'capture')
    assert result['status']=='no_accessible_evidence' and not result['dossier_generated']
    assert result['model_calls']==0 and not result['human_approval']
    assert json.loads((tmp_path/'capture/sources.json').read_text())==[]
