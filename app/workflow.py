"""Deterministic workflow policy. No model can approve or execute external actions."""
from dataclasses import dataclass

TRANSITIONS = {
    ("brand_created", "queue_crawl"): "crawl_queued",
    ("crawl_queued", "start_crawl"): "crawling",
    ("crawling", "crawl_succeeded"): "awaiting_onboarding",
    ("crawling", "crawl_failed"): "crawl_failed",
    ("crawl_failed", "retry"): "crawl_queued",
    ("awaiting_onboarding", "queue_crawl"): "crawl_queued",
    ("awaiting_onboarding", "submit_onboarding"): "awaiting_profile_confirmation",
    ("awaiting_profile_confirmation", "confirm_profile"): "ready_for_research",
    ("ready_for_research", "queue_research"): "research_queued",
    ("research_queued", "start_research"): "researching",
    ("researching", "research_succeeded"): "awaiting_research_confirmation",
    ("awaiting_research_confirmation", "confirm_research"): "ready_for_strategy",
    ("awaiting_research_confirmation", "request_research_revision"): "ready_for_research",
    ("researching", "provider_outcome_unknown"): "needs_human_review",
    ("researching", "budget_exhausted"): "needs_human_review",
    ("ready_for_strategy", "queue_strategy"): "strategy_queued",
    ("strategy_queued", "queue_strategy"): "strategy_queued",
    ("red_team_queued", "queue_red_team"): "red_team_queued",
    ("plan_queued", "queue_plan"): "plan_queued",
    ("creative_queued", "queue_creative"): "creative_queued",
    ("strategy_queued", "start_strategy"): "strategizing",
    ("strategizing", "strategy_succeeded"): "red_team_queued",
    ("red_team_queued", "start_red_team"): "reviewing_strategy",
    ("reviewing_strategy", "critique_succeeded"): "awaiting_strategy_selection",
    ("awaiting_strategy_selection", "request_strategy_revision"): "strategy_queued",
    ("awaiting_strategy_selection", "approve_strategy"): "plan_queued",
    ("plan_queued", "start_plan"): "planning",
    ("planning", "plan_succeeded"): "awaiting_plan_approval",
    ("awaiting_plan_approval", "request_plan_revision"): "plan_queued",
    ("awaiting_plan_approval", "approve_plan"): "creative_queued",
    ("creative_queued", "start_creative"): "creating_briefs",
    ("creating_briefs", "creative_succeeded"): "awaiting_creative_approval",
    ("awaiting_creative_approval", "request_creative_revision"): "creative_queued",
    ("awaiting_creative_approval", "approve_creative"): "ready_for_delivery",
    ("report_queued", "start_report"): "preparing_deliverables",
    ("preparing_deliverables", "report_succeeded"): "awaiting_deliverable_approval",
    ("awaiting_deliverable_approval", "request_deliverable_revision"): "report_queued",
    ("awaiting_deliverable_approval", "approve_deliverables"): "completed",
}
for _running in ['strategizing','reviewing_strategy','planning','creating_briefs']:
    for _event in ['provider_outcome_unknown','budget_exhausted']:
        TRANSITIONS[(_running,_event)]='needs_human_review'
RUNNING = {"crawling", "researching", "strategizing", "reviewing_strategy", "planning", "creating_briefs", "preparing_deliverables"}
TERMINAL = {"completed", "cancelled"}
STATES = {s for pair, target in TRANSITIONS.items() for s in (*pair[:1], target)} | {"needs_human_review", "awaiting_inputs", "failed", "cancelled"}
HUMAN_EVENTS = {"submit_onboarding", "confirm_profile", "confirm_research", "approve_strategy", "approve_plan", "approve_creative", "approve_deliverables", "resume_after_review", "inputs_provided", "request_strategy_revision", "request_plan_revision", "request_creative_revision"}
RESUME = {"crawling": "crawl_queued", "researching": "research_queued", "strategizing": "strategy_queued", "reviewing_strategy": "red_team_queued", "planning": "plan_queued", "creating_briefs": "creative_queued", "preparing_deliverables": "report_queued"}


class TransitionError(ValueError):
    pass


@dataclass(frozen=True)
class GateContext:
    human: bool = False
    critical_gaps: bool = False
    confirmed_profile: bool = False
    validated_dossier: bool = False
    exact_version_approved: bool = False
    red_team_clear: bool = False
    kpis_approved: bool = False
    dependencies_current: bool = False
    validation_failures: int = 0
    calls: int = 0
    max_calls: int = 20
    tokens: int = 0
    token_budget: int = 100000
    resume_state: str | None = None


def next_state(state: str, event: str, gate: GateContext) -> str:
    if state in TERMINAL:
        raise TransitionError("terminal workflows cannot resume")
    if event == "cancel":
        if not gate.human:
            raise TransitionError("cancellation requires a human")
        return "cancelled"
    if event in HUMAN_EVENTS and not gate.human:
        raise TransitionError("human decision required")
    if event == "validation_failed" and state in RUNNING:
        return "needs_human_review" if gate.validation_failures >= 1 else state
    if event == "run_failed" and state in RUNNING:
        return "failed"
    if event == "request_inputs" and state in RUNNING:
        return "awaiting_inputs"
    if event in {"resume_after_review", "inputs_provided", "retry"} and state in {"needs_human_review", "awaiting_inputs", "failed"}:
        if not gate.human or gate.resume_state not in RESUME.values():
            raise TransitionError("reviewed resume target required")
        if gate.calls >= gate.max_calls or gate.tokens >= gate.token_budget:
            raise TransitionError("workflow budget exhausted")
        return gate.resume_state
    target = TRANSITIONS.get((state, event))
    if target is None:
        raise TransitionError("event is not allowed in this state")
    if target.endswith("_queued") and (gate.calls >= gate.max_calls or gate.tokens >= gate.token_budget):
        raise TransitionError("workflow budget exhausted")
    if event in {"confirm_profile", "queue_research", "confirm_research"}:
        if gate.critical_gaps or not gate.confirmed_profile:
            raise TransitionError("a confirmed profile without critical gaps is required")
    if event == "confirm_research" and not gate.validated_dossier:
        raise TransitionError("validated research is required")
    if event == "approve_strategy" and not (gate.exact_version_approved and gate.red_team_clear and gate.kpis_approved and gate.dependencies_current):
        raise TransitionError("strategy, KPI and red team gates are incomplete")
    if event in {"approve_plan", "approve_creative", "approve_deliverables"} and not (gate.exact_version_approved and gate.dependencies_current):
        raise TransitionError("current artifact approval required")
    return target
