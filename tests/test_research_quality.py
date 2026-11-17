"""Annotated provenance failures plus the explicit limits of deterministic validation."""
import copy
import json
from pathlib import Path
from uuid import UUID, uuid4
import pytest
from app.research_models import ResearchReview, ResearchDossierV11
from app.services.model_provider import ModelRequest
from app.services.research_validation import evaluate_output, validate_review, ResearchValidationError
from tests.test_research import RecordedProvider, TEXT, human_review

CASES=json.loads((Path(__file__).parent/'fixtures/research_quality_cases.json').read_text())
CUTOFF='2026-11-16'


def sample():
    brand=uuid4()
    rows=[]
    for area in ['brand','market','audience']:
        rows.append({'id':uuid4(),'area':area,'kind':'public_web','confidence_cap':.85,'extracted_text':TEXT,
            'citation':{'source_id':str(uuid4()),'url':'https://example.com/'+area,'publisher':'Example fixture',
                'published_at':None,'accessed_at':CUTOFF+'T10:00:00Z','geographic_scope':'india',
                'applicability_to_india':None,'access_limitations':['Controlled fixture; not real brand research.']}})
    ctx={'brand_id':str(brand),'profile_version':1,'research_as_of':CUTOFF,'evidence':rows}
    # The provider only needs the supplied fields, not internal UUID objects.
    ctx['evidence']=[{**r,'id':str(r['id'])} for r in rows]
    request=ModelRequest(task_type='brand_research',template_version=2,context=[],system='Research only.',
        context_json=json.dumps(ctx),output_schema_json='{}',output_schema_name='research-dossier-v1.1',
        maximum_input_tokens=16000,maximum_output_tokens=4096)
    payload=json.loads(RecordedProvider().generate(request).output_json)
    return brand,rows,payload


def mutate(case,rows,payload):
    claim=payload['brand_findings'][0]
    if case=='extra_citation_without_quote':
        claim['source_ids'].append(rows[1]['citation']['source_id'])
    elif case=='assumption_as_supported_coverage':
        claim['classification']='assumption';claim['source_ids']=[]
        payload['claim_support']=[s for s in payload['claim_support'] if s['claim_id']!=claim['claim_id']]
    elif case=='duplicate_support':payload['claim_support'].append(copy.deepcopy(payload['claim_support'][0]))
    elif case=='missing_quote':payload['claim_support'][0]['quote']='These words did not occur in the supplied text.'
    elif case=='duplicate_gap':payload['missing_inputs'].append(copy.deepcopy(payload['missing_inputs'][0]))
    elif case=='duplicate_citation':claim['source_ids']*=2
    elif case=='duplicate_coverage':payload['coverage'][0]['finding_ids']*=2
    elif case=='different_area_coverage':payload['coverage'][0]['finding_ids']=[payload['category_market'][0]['claim_id']]
    elif case=='hidden_finding':
        payload['coverage'][1].update(status='unavailable',finding_ids=[],limitations=['Pretends no evidence is present.'])
        payload['missing_inputs'].append({'field':'market','question':'Provide market evidence','critical':False,'reason':'Hides existing findings.'})
    elif case=='global_evidence_as_india_fact':
        rows[0]['citation'].update(geographic_scope='global',applicability_to_india='Global context only; India applicability remains unverified.')
        payload['sources'][0]=copy.deepcopy(rows[0]['citation'])
    elif case=='duplicate_conflict_claim':
        payload['conflict_pairs']=[{'conflict_id':str(uuid4()),'claim_ids':[claim['claim_id']]*2,'explanation':'Invalid repeat','critical':True}]
    elif case=='dangling_support':payload['claim_support'][0]['claim_id']=str(uuid4())
    elif case=='wrong_research_cutoff':payload['research_as_of']='2026-11-15'
    elif case=='invented_market_share_real_quote':claim['statement']='This brand has 90 percent of the Indian market.'
    elif case=='injection_requested_approval_field':payload['decision']='approved'
    return json.dumps(payload)[:-12] if case=='truncated_json' else json.dumps(payload)


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['id'])
def test_annotated_quality_cases(case):
    brand,rows,payload=sample()
    raw=mutate(case['id'],rows,payload)
    dossier,report=evaluate_output(raw,brand,1,rows,CUTOFF)
    assert report.issue_codes==([case['expected_issue']] if case['expected_issue'] else []),case['reason']
    assert report.semantic_status=='not_assessed'
    assert bool(dossier)==(case['expected_issue'] is None)
    if dossier:
        semantic=human_review(dossier.model_dump(mode='json'))
        semantic['claim_assessments'][0]['verdict']=case['gold_semantic_verdict']
        semantic['claim_assessments'][0]['rationale']=case['reason']
        review=ResearchReview(expected_workflow_version=1,artifact_sha256='a'*64,decision='approved',
            reason='Controlled human annotation',semantic_review=semantic)
        if case['gold_semantic_verdict']=='supported':
            validate_review(dossier,review)
        else:
            with pytest.raises(ResearchValidationError,match='Unsupported or uncertain'):
                validate_review(dossier,review)


def test_approval_requires_complete_human_assessment_and_no_open_issues():
    brand,rows,payload=sample()
    dossier,report=evaluate_output(json.dumps(payload),brand,1,rows,CUTOFF)
    base={'expected_workflow_version':1,'artifact_sha256':'a'*64,'decision':'approved','reason':'Human review'}
    for semantic,code in [(None,'review_semantic_required'),
        ({**human_review(payload),'claim_assessments':[]},'review_claims_incomplete'),
        ({**human_review(payload),'unresolved_issues':['The claimed market size has no representative evidence.']},'review_semantic_unresolved')]:
        with pytest.raises(ResearchValidationError) as exc:
            validate_review(dossier,ResearchReview(**base,semantic_review=semantic))
        assert exc.value.code==code


def test_registered_but_unrelated_evidence_cannot_resolve_a_gap():
    brand,rows,payload=sample()
    dossier,_=evaluate_output(json.dumps(payload),brand,1,rows,CUTOFF)
    claim=dossier.brand_findings[0]
    gap={'field':'social','kind':'evidence','answer':'Attempted resolution using brand evidence.',
        'source_ids':claim.source_ids,'claim_ids':[claim.claim_id],'rationale':'A registered source is not evidence for every topic.'}
    review=ResearchReview(expected_workflow_version=1,artifact_sha256='a'*64,decision='approved',reason='Controlled test',
        semantic_review=human_review(payload),gap_resolutions=[gap])
    with pytest.raises(ResearchValidationError) as exc:validate_review(dossier,review)
    assert exc.value.code=='review_gap_area_mismatch'


def test_all_conflict_sources_must_support_the_selected_claim():
    brand,rows,payload=sample()
    a,b=payload['brand_findings'][0],payload['category_market'][0]
    conflict={'conflict_id':str(uuid4()),'claim_ids':[a['claim_id'],b['claim_id']],'explanation':'Controlled conflicting observations','critical':True}
    payload['conflict_pairs']=[conflict]
    dossier,_=evaluate_output(json.dumps(payload),brand,1,rows,CUTOFF)
    review=ResearchReview(expected_workflow_version=1,artifact_sha256='a'*64,decision='approved',reason='Controlled test',
        semantic_review=human_review(payload),conflict_resolutions=[{'conflict_id':conflict['conflict_id'],
            'selected_claim_id':a['claim_id'],'source_ids':a['source_ids']+b['source_ids'],'rationale':'Mixes an unrelated citation with a valid one.'}])
    with pytest.raises(ResearchValidationError) as exc:validate_review(dossier,review)
    assert exc.value.code=='review_conflict_source_unrelated'


def test_revision_can_record_an_unsupported_finding_without_approving_it():
    brand,rows,payload=sample()
    dossier,_=evaluate_output(json.dumps(payload),brand,1,rows,CUTOFF)
    semantic=human_review(payload)
    semantic['claim_assessments'][0]['verdict']='unsupported'
    review=ResearchReview(expected_workflow_version=1,artifact_sha256='a'*64,decision='revision_requested',reason='The quote does not support the assertion.',semantic_review=semantic)
    validate_review(dossier,review)


def test_unknown_dates_and_old_publications_are_warnings_not_fabricated_dates():
    brand,rows,payload=sample()
    rows[0]['citation']['published_at']='2020-01-01'
    payload['sources'][0]=copy.deepcopy(rows[0]['citation'])
    dossier,report=evaluate_output(json.dumps(payload),brand,1,rows,CUTOFF)
    assert dossier is not None
    codes={w.code for w in report.evidence_warnings}
    assert {'publication_date_unknown','publication_older_than_365_days'}<=codes
