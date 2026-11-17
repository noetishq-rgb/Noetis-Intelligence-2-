"""Phase 4: immutable proposals and explicit founder/admin decisions."""
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID
from pydantic import Field
from app.models import (StrictModel, ShortText, Text, StrategyOptions, StrategyOption,
    RedTeamReview, ThirtyDayPlan, PlanTask, CreativeBrief, KPI)

Stage = Literal['strategy','strategy_critique','thirty_day_plan','creative_brief']
Format = Literal['reel','carousel','static','video','linkedin_post','landing_page','seo_article','email']


class StrategySet(StrategyOptions):
    schema_version: Literal['1.1'] = '1.1'
    options: Annotated[list[StrategyOption], Field(min_length=3,max_length=3)]


class DeliveryTask(PlanTask):
    format: Format
    channel: Literal['meta_ads','meta_organic','google_seo','google_search','instagram','linkedin','youtube','facebook','email']


class DeliveryPlan(ThirtyDayPlan):
    schema_version: Literal['1.1'] = '1.1'
    tasks: Annotated[list[DeliveryTask], Field(min_length=1,max_length=8)]


class CreativeBatch(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    brand_id: UUID
    plan_id: UUID
    briefs: Annotated[list[CreativeBrief],Field(min_length=1,max_length=8)]


class ArtifactRef(StrictModel):
    artifact_id: UUID
    version: Annotated[int,Field(ge=1)]
    sha256: Annotated[str,Field(pattern=r'^[a-f0-9]{64}$')]
    kind: ShortText


class PlanningStart(StrictModel):
    stage: Stage
    expected_workflow_version: Annotated[int,Field(ge=1)]
    starts_on: date | None = None
    owner_user_ids: Annotated[list[UUID],Field(max_length=20)] = Field(default_factory=list)
    acknowledge_uncertain_charge: bool = False


class IssueAcknowledgement(StrictModel):
    issue_index: Annotated[int,Field(ge=0,le=50)]
    rationale: Text
    accepted_risk: bool


class PlanningReview(StrictModel):
    expected_workflow_version: Annotated[int,Field(ge=1)]
    artifact_sha256: Annotated[str,Field(pattern=r'^[a-f0-9]{64}$')]
    decision: Literal['approved','revision_requested','rejected']
    reason: Text
    evidence_checked: bool = False
    dependencies_checked: bool = False
    distinctness_checked: bool = False
    owners_and_schedule_checked: bool = False
    budget_checked: bool = False
    compliance_checked: bool = False
    selected_option_id: UUID | None = None
    critique_sha256: Annotated[str,Field(pattern=r'^[a-f0-9]{64}$')] | None = None
    approved_kpis: Annotated[list[KPI],Field(max_length=20)] = Field(default_factory=list)
    issue_acknowledgements: list[IssueAcknowledgement] = Field(default_factory=list)


class PlanningValidation(StrictModel):
    validator_version: Literal['4.0'] = '4.0'
    checked_at: datetime
    valid: bool
    issue_codes: list[ShortText]
    semantic_review_required: Literal[True] = True


class PlanningReviewView(StrictModel):
    id: UUID
    reviewer_id: UUID
    reviewed_at: datetime
    decision: Literal['approved','revision_requested','rejected']
    approval_id: UUID
    details: PlanningReview


class PlanningOutputView(StrictModel):
    id: UUID
    artifact: ArtifactRef
    stage: Stage
    parents: list[ArtifactRef]
    content: StrategySet | RedTeamReview | DeliveryPlan | CreativeBatch
    validation: PlanningValidation
    review: PlanningReviewView | None = None


class PlanningAccepted(StrictModel):
    brand_id: UUID
    job_id: UUID
    workflow_id: UUID
    stage: Stage
    state: str


class PlanningRunView(StrictModel):
    job_id: UUID
    stage: Stage
    status: str
    attempts: int
    last_error: str | None
    output_id: UUID | None
    latest_validation: PlanningValidation | None
    budget_totals_scope: Literal['workflow'] = 'workflow'
    model_calls: int
    tokens_used: int
    tokens_reserved: int
    cost_used_paise: int
    cost_reserved_paise: int
    pricing_complete: bool
    currency: Literal['INR'] = 'INR'

MODELS={'strategy':StrategySet,'strategy_critique':RedTeamReview,'thirty_day_plan':DeliveryPlan,'creative_brief':CreativeBatch}
SCHEMAS={'strategy':'strategy-options-v1.1','strategy_critique':'red-team-review','thirty_day_plan':'30-day-plan-v1.1','creative_brief':'creative-batch'}
STAGES={
 'strategy':('strategy_queued','strategizing','start_strategy','strategy_succeeded','strategy_director'),
 'strategy_critique':('red_team_queued','reviewing_strategy','start_red_team','critique_succeeded','red_team_critic'),
 'thirty_day_plan':('plan_queued','planning','start_plan','plan_succeeded','execution_planner'),
 'creative_brief':('creative_queued','creating_briefs','start_creative','creative_succeeded','creative_strategist'),
}
