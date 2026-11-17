import json
from pathlib import Path
from uuid import uuid4
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from app.models import Claim, Money, OptionReview, ApprovalRecord, SCHEMA_MODELS
from app.services.extraction import extract_profile
from app.services.validation import validate_output
from app.workflow import GateContext, next_state, TransitionError


def test_all_schemas_are_valid_and_objects_are_closed():
    def walk(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    for name, model in SCHEMA_MODELS.items():
        schema = model.model_json_schema()
        Draft202012Validator.check_schema(schema)
        walk(schema)


def test_unsupported_fields_wrong_tenant_and_unregistered_sources_are_rejected():
    source, brand = uuid4(), uuid4()
    profile = extract_profile(brand, 1, {"name": "Example", "website_url": "https://example.com"}, [], source)
    payload = profile.model_dump(mode="json")
    validate_output("brand-profile", payload, brand, {source})
    with pytest.raises(ValueError):
        validate_output("brand-profile", payload, uuid4(), {source})
    with pytest.raises(ValueError):
        validate_output("brand-profile", payload, brand, set())
    with pytest.raises(Exception):
        validate_output("brand-profile", {**payload, "internal_secret": "unexpected"}, brand)
    assert profile.budget_range is None
    assert any(gap.field == "budget_range" and gap.critical for gap in profile.missing_inputs)


def test_verified_claim_requires_sources_and_currency_is_inr():
    with pytest.raises(ValidationError):
        Claim(claim_id=uuid4(), classification="fact", statement="Unsupported", source_ids=[], confidence=.9, rationale="None", geographic_scope="india")
    with pytest.raises(ValidationError):
        Money(currency="USD", amount_paise=100)
    with pytest.raises(ValidationError):
        Money(currency="INR", amount_paise=True)


def test_critical_inputs_and_human_approval_block_strategy_advancement():
    with pytest.raises(TransitionError):
        next_state("ready_for_research", "queue_research", GateContext(confirmed_profile=True, critical_gaps=True))
    with pytest.raises(TransitionError):
        next_state("awaiting_strategy_selection", "approve_strategy", GateContext(exact_version_approved=True, red_team_clear=True, kpis_approved=True, dependencies_current=True))
    with pytest.raises(TransitionError):
        next_state("awaiting_strategy_selection", "approve_strategy", GateContext(human=True, exact_version_approved=True, red_team_clear=False, kpis_approved=True, dependencies_current=True))
    assert next_state("awaiting_strategy_selection", "approve_strategy", GateContext(human=True, exact_version_approved=True, red_team_clear=True, kpis_approved=True, dependencies_current=True)) == "plan_queued"


def test_two_validation_failures_pause_and_budget_exhaustion_stops_queuing():
    assert next_state("researching", "validation_failed", GateContext(validation_failures=0)) == "researching"
    assert next_state("researching", "validation_failed", GateContext(validation_failures=1)) == "needs_human_review"
    with pytest.raises(TransitionError):
        next_state("ready_for_research", "queue_research", GateContext(confirmed_profile=True, calls=20))
    with pytest.raises(TransitionError):
        next_state("completed", "retry", GateContext(human=True))


def test_exported_contracts_match_model_source():
    root = Path(__file__).resolve().parents[1]
    for name, model in SCHEMA_MODELS.items():
        stored = json.loads((root / "schemas" / (name + ".schema.json")).read_text())
        stored.pop("$schema")
        stored.pop("$id")
        assert stored == model.model_json_schema()


def test_research_schemas_and_current_api_contract_are_generated_from_source():
    from app.research_models import ResearchDossierV11, ResearchStart, ResearchReview, PromptCompile, ResearchValidationReport
    from app.main import app
    root = Path(__file__).resolve().parents[1]
    for name, model in {'research-dossier-v1.1':ResearchDossierV11,'research-start':ResearchStart,
                        'research-review-v2':ResearchReview,'research-prompt-compile':PromptCompile,
                        'research-validation-report':ResearchValidationReport}.items():
        schema=json.loads((root/'schemas'/(name+'.schema.json')).read_text())
        Draft202012Validator.check_schema(schema)
        schema.pop('$schema');schema.pop('$id')
        assert schema==model.model_json_schema()
    assert json.loads((root/'docs/openapi-brand-brain.json').read_text())==app.openapi()


def test_plan_dependency_cycles_and_minor_only_critiques_are_rejected():
    from app.models import ThirtyDayPlan, PlanTask, KPI, ReviewIssue
    task_id = uuid4()
    metric = KPI(name="Qualified enquiries", definition="Enquiries meeting agreed criteria", unit="count", baseline=None, target_min=None, target_max=None, measurement_source="Reviewed lead register", review_day=30)
    task = PlanTask(task_id=task_id, day=1, channel="google_seo", deliverable="A reviewed landing page brief", format="document", audience_segment_id=None, funnel_stage="consideration", owner_user_id=uuid4(), dependency_ids=[task_id], approval_required=True, success_metric=metric, production_requirements=[], risks=[])
    with pytest.raises(ValidationError, match="cycle"):
        ThirtyDayPlan(brand_id=uuid4(), approved_strategy_id=uuid4(), approval_record_id=uuid4(), starts_on="2026-11-16", weekly_themes=[], tasks=[task], test_matrix=[], budget=None, risks=[], missing_inputs=[])
    issues = [ReviewIssue(severity="minor", issue="Minor formatting issue", evidence=[], required_revision="Review wording") for _ in range(3)]
    with pytest.raises(ValidationError, match="material"):
        OptionReview(option_id=uuid4(), verdict="approve", issues=issues, weak_assumptions=[], missing_evidence=[], competitor_countermove="Competitors could adjust their offer", client_questions=[], risk_score=.3)
