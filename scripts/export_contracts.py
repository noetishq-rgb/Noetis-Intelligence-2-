import json
import sys
from pathlib import Path
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from app.client_access import ClientCreate, ClientView, AccountAction, PasswordChange
from app.models import SCHEMA_MODELS, OnboardingInput
from app.research_models import ResearchDossierV11, ResearchStart, ResearchReview, PromptCompile, ResearchValidationReport
from app.main import app
from app.brain_models import SetupDraft,FindingReview,BrainConfirm,BrainDocument,BrainStage
from app.models import BrandProfileV11
from app.workflow import TRANSITIONS, STATES, RUNNING, RESUME
from app.planning_models import StrategySet,DeliveryPlan,CreativeBatch,PlanningStart,PlanningReview,PlanningValidation


def write(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


for name, model in {**SCHEMA_MODELS, "onboarding-input": OnboardingInput}.items():
    schema = model.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:marketing-intelligence:" + name + ":1.0"
    write(root / "schemas" / (name + ".schema.json"), schema)
for name, model, version in [
    ("client-create", ClientCreate, "1.0"),
    ("client-account", ClientView, "1.0"),
    ("client-access-action", AccountAction, "1.0"),
    ("password-change", PasswordChange, "1.0"),
    ("brand-profile-v1.1", BrandProfileV11, "1.1"),
    ("brain-setup", SetupDraft, "1.0"),
    ("brain-finding-review", FindingReview, "1.0"),
    ("brain-confirmation", BrainConfirm, "1.0"),
    ("brand-brain", BrainDocument, "1.0"),
    ("brain-stage", BrainStage, "1.0"),
    ("research-dossier-v1.1", ResearchDossierV11, "1.1"),
    ("research-start", ResearchStart, "1.0"),
    ("research-review-v2", ResearchReview, "2.0"),
    ("research-validation-report", ResearchValidationReport, "1.0"),
    ("research-prompt-compile", PromptCompile, "1.0"),
    ("strategy-options-v1.1", StrategySet, "1.1"),
    ("30-day-plan-v1.1", DeliveryPlan, "1.1"),
    ("creative-batch", CreativeBatch, "1.0"),
    ("planning-start", PlanningStart, "1.0"),
    ("planning-review", PlanningReview, "1.0"),
    ("planning-validation", PlanningValidation, "1.0"),
]:
    schema = model.model_json_schema()
    schema.update({"$schema": "https://json-schema.org/draft/2020-12/schema",
                   "$id": "urn:marketing-intelligence:" + name + ":" + version})
    write(root / "schemas" / (name + ".schema.json"), schema)
# Keep delivered Phase 1/2 snapshots; the current API includes Phase 3 checks.
write(root / "docs" / "openapi-brand-brain.json", app.openapi())
write(root / "docs" / "workflow-state-machine.json", {
    "version": "1.3", "initial_state": "brand_created", "states": sorted(STATES),
    "transitions": [{"from": origin, "event": event, "to": target} for (origin,event),target in TRANSITIONS.items()],
    "running_states": sorted(RUNNING), "resume_targets": RESUME,
    "validation_policy": {"maximum_invalid_outputs": 2, "then": "needs_human_review"},
    "job_policy": {"max_attempts": 3, "crawl_lease_seconds": 120, "research_lease_seconds": 900, "fenced_completion": True,
                   "lost_model_dispatch": "hold_reservation_and_pause_for_human_review"},
    "budget_policy": {"max_agent_calls": 20, "token_budget": 100000, "atomic_reservation": True,
                      "unknown_usage_is_zero": False, "currency": "INR", "cost_cap_requires_dated_pricing": True},
    "phase1_enabled_stages": ["intake", "crawl", "onboarding", "profile_confirmation"],
    "phase2_enabled_stages": ["research_collection", "research_generation", "research_confirmation"],
    "phase2_stop_state": "ready_for_strategy",
    "phase3_validation": {"validator_version": "3.0", "human_claim_assessment_required": True,
                          "provenance_is_semantic_validation": False},
    "phase4_enabled_stages": ["strategy", "strategy_critique", "thirty_day_plan", "creative_brief"],
    "phase4_stop_state": "ready_for_delivery",
    "phase4_model_calls_default": "disabled",
    "external_execution": False})
print("Exported current Brand Brain contracts and state machine; earlier API snapshots preserved.")
