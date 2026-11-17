"""Canonical contracts. Unknown values stay null and must be explained in gaps."""
from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl, model_validator

Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Text = Annotated[str, Field(min_length=1, max_length=10000)]
ShortText = Annotated[str, Field(min_length=1, max_length=300)]
Language = Annotated[str, Field(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Role(StrEnum):
    founder = "founder"
    admin = "admin"
    team = "team"
    client = "client"
    service = "service"


class Money(StrictModel):
    currency: Literal["INR"] = "INR"
    amount_paise: Annotated[int, Field(ge=0, strict=True)]


class MoneyRange(StrictModel):
    minimum: Money
    maximum: Money

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.minimum.amount_paise > self.maximum.amount_paise:
            raise ValueError("minimum exceeds maximum")
        return self


class Gap(StrictModel):
    field: ShortText
    question: Text
    critical: bool
    reason: Text


class Citation(StrictModel):
    source_id: UUID
    url: HttpUrl
    publisher: ShortText | None
    published_at: date | None
    accessed_at: datetime
    geographic_scope: Literal["india", "global", "unknown"]
    applicability_to_india: Text | None
    access_limitations: list[ShortText]

    @model_validator(mode="after")
    def global_context(self) -> Self:
        if self.geographic_scope == "global" and not self.applicability_to_india:
            raise ValueError("global sources require India applicability")
        return self


class Claim(StrictModel):
    claim_id: UUID
    classification: Literal["fact", "inference", "assumption", "gap"]
    statement: Text
    source_ids: list[UUID]
    confidence: Confidence
    rationale: Text
    geographic_scope: Literal["india", "global", "unknown"]
    model_config = ConfigDict(extra="forbid", json_schema_extra={"allOf": [
        {"if": {"properties": {"classification": {"enum": ["fact", "inference"]}}, "required": ["classification"]},
         "then": {"properties": {"source_ids": {"minItems": 1}}}},
        {"if": {"properties": {"classification": {"const": "gap"}}, "required": ["classification"]},
         "then": {"properties": {"confidence": {"const": 0}}}}
    ]})

    @model_validator(mode="after")
    def evidence(self) -> Self:
        if self.classification in {"fact", "inference"} and not self.source_ids:
            raise ValueError("facts and inferences need evidence")
        if self.classification == "gap" and self.confidence != 0:
            raise ValueError("unknown information has zero confidence")
        return self


class ObservedText(StrictModel):
    value: Text
    evidence: Claim


class Product(StrictModel):
    name: ObservedText
    description: ObservedText | None
    price: Money | None
    price_evidence: Claim | None

    @model_validator(mode="after")
    def pricing(self) -> Self:
        if self.price is not None and self.price_evidence is None:
            raise ValueError("pricing requires evidence")
        return self


class OnboardingQuestion(StrictModel):
    field: ShortText
    label: Text
    required: bool
    input_type: Literal["text", "long_text", "money_range", "list", "url_list"]
    reason: Text


class BrandProfile(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    brand_id: UUID
    version: Annotated[int, Field(ge=1)]
    name: ObservedText | None
    website_url: HttpUrl
    business_description: ObservedText | None
    business_model: ObservedText | None
    positioning: ObservedText | None
    products_services: list[Product]
    target_geographies: list[ShortText]
    geography_evidence: list[Claim]
    audience_description: ObservedText | None
    objectives: list[ShortText]
    budget_range: MoneyRange | None
    pricing_margin_notes: ObservedText | None
    constraints: list[ShortText]
    languages: list[Language]
    competitors: list[ShortText]
    conversion_paths: list[HttpUrl]
    proof_points: list[Claim]
    sources: list[Citation]
    missing_inputs: list[Gap]
    onboarding_questions: list[OnboardingQuestion]
    data_quality_warnings: list[Text]
    approval_status: Literal["draft", "confirmed"] = "draft"

    @model_validator(mode="after")
    def known_sources(self) -> Self:
        validate_source_references(self, {c.source_id for c in self.sources})
        return self


class BrandProfileV11(BrandProfile):
    # Legacy v1.0 schemas remain frozen. Social-first intake has no invented website.
    schema_version: Literal["1.0", "1.1"] = "1.0"
    website_url: HttpUrl | None


class AudienceSegment(StrictModel):
    segment_id: UUID
    name: ShortText
    geography: list[ShortText]
    languages: list[Language]
    needs: list[Claim]
    objections: list[Claim]
    buying_triggers: list[Claim]
    evidence: list[Claim]


class CompetitorProfile(StrictModel):
    competitor_id: UUID
    name: ShortText
    website: HttpUrl | None
    relationship: Literal["direct", "indirect"]
    positioning: list[Claim]
    offers_pricing: list[Claim]
    content_channels: list[Claim]


class ResearchDossier(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    brand_id: UUID
    profile_version: Annotated[int, Field(ge=1)]
    executive_summary: Text
    geographic_focus: Literal["IN"] = "IN"
    research_as_of: date
    sources: list[Citation]
    brand_findings: list[Claim]
    category_market: list[Claim]
    demand_seasonality: list[Claim]
    audiences: list[AudienceSegment]
    competitors: list[CompetitorProfile]
    social_listening: list[Claim]
    reputation_reviews: list[Claim]
    search_opportunities: list[Claim]
    creative_content_gaps: list[Claim]
    regulatory_cultural_risks: list[Claim]
    contradictions: list[Claim]
    missing_inputs: list[Gap]
    client_questions: list[Text]
    next_action: Text

    @model_validator(mode="after")
    def known_sources(self) -> Self:
        validate_source_references(self, {c.source_id for c in self.sources})
        return self


class KPI(StrictModel):
    name: ShortText
    definition: Text
    unit: Literal["count", "percent", "ratio", "INR", "days"]
    baseline: float | None
    target_min: float | None
    target_max: float | None
    measurement_source: Text
    review_day: Annotated[int, Field(ge=1, le=30)]


class ChannelAllocation(StrictModel):
    channel: Literal["meta_ads", "meta_organic", "google_seo", "google_search", "instagram", "linkedin", "youtube", "facebook", "email"]
    percent: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
    rationale: Text
    evidence: list[Claim]


class StrategyOption(StrictModel):
    option_id: UUID
    name: ShortText
    thesis: Text
    primary_icp: AudienceSegment
    core_offer: Text
    positioning: Text
    messaging: list[Text]
    channel_allocation: Annotated[list[ChannelAllocation], Field(min_length=1)]
    funnel: list[Text]
    kpis: Annotated[list[KPI], Field(min_length=1)]
    expected_revenue_range: MoneyRange | None
    forecast_assumptions: list[Text]
    required_assets_data: list[Text]
    execution_priorities: list[Text]
    assumptions: list[Claim]
    risks: Annotated[list[Text], Field(min_length=1)]
    tradeoffs: Annotated[list[Text], Field(min_length=1)]
    failure_scenarios: Annotated[list[Text], Field(min_length=1)]
    evidence: list[Claim]
    confidence: Confidence

    @model_validator(mode="after")
    def allocation(self) -> Self:
        if abs(sum(c.percent for c in self.channel_allocation) - 100) > .001:
            raise ValueError("channel allocation must sum to 100")
        if self.expected_revenue_range and not self.forecast_assumptions:
            raise ValueError("forecasts need explicit assumptions")
        return self


class StrategyOptions(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    brand_id: UUID
    approved_profile_id: UUID
    dossier_id: UUID
    options: Annotated[list[StrategyOption], Field(min_length=2, max_length=3)]
    recommended_option_id: UUID
    recommendation_reason: Text
    contrarian_view: Text
    decision_required: Text
    missing_inputs: list[Gap]

    @model_validator(mode="after")
    def distinct(self) -> Self:
        ids = [s.option_id for s in self.options]
        if len(set(ids)) != len(ids) or len({s.name.casefold() for s in self.options}) != len(ids):
            raise ValueError("options need unique identities and names")
        if self.recommended_option_id not in ids:
            raise ValueError("recommendation must reference an option")
        return self


class ReviewIssue(StrictModel):
    severity: Literal["blocking", "material", "minor"]
    issue: Text
    evidence: list[Claim]
    required_revision: Text


class OptionReview(StrictModel):
    option_id: UUID
    verdict: Literal["approve", "revise", "reject"]
    issues: Annotated[list[ReviewIssue], Field(min_length=3)]
    weak_assumptions: list[Text]
    missing_evidence: list[Text]
    competitor_countermove: Text
    client_questions: list[Text]
    risk_score: Confidence

    @model_validator(mode="after")
    def material(self) -> Self:
        if sum(i.severity in {"blocking", "material"} for i in self.issues) < 3:
            raise ValueError("three material or blocking weaknesses are required")
        if self.verdict == "approve" and any(i.severity == "blocking" for i in self.issues):
            raise ValueError("a blocking issue prevents approval")
        return self


class RedTeamReview(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    brand_id: UUID
    strategy_artifact_id: UUID
    reviews: Annotated[list[OptionReview], Field(min_length=2, max_length=3)]
    missing_inputs: list[Gap]


class PlanTask(StrictModel):
    task_id: UUID
    day: Annotated[int, Field(ge=1, le=30)]
    channel: ShortText
    deliverable: Text
    format: ShortText
    audience_segment_id: UUID | None
    funnel_stage: Literal["awareness", "consideration", "conversion", "retention", "measurement"]
    owner_user_id: UUID
    dependency_ids: list[UUID]
    approval_required: bool
    success_metric: KPI
    production_requirements: list[Text]
    risks: list[Text]


class WeekTheme(StrictModel):
    week: Annotated[int, Field(ge=1, le=5)]
    theme: Text


class Experiment(StrictModel):
    hypothesis: Text
    control: Text
    variant: Text
    success_metric: KPI
    checkpoint_days: Annotated[list[Annotated[int, Field(ge=1, le=30)]], Field(min_length=1)]


class ThirtyDayPlan(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    brand_id: UUID
    approved_strategy_id: UUID
    approval_record_id: UUID
    starts_on: date
    duration_days: Literal[30] = 30
    weekly_themes: list[WeekTheme]
    tasks: Annotated[list[PlanTask], Field(min_length=1)]
    test_matrix: list[Experiment]
    budget: MoneyRange | None
    risks: list[Text]
    missing_inputs: list[Gap]

    @model_validator(mode="after")
    def dependencies(self) -> Self:
        by_id = {t.task_id: t for t in self.tasks}
        if len(by_id) != len(self.tasks):
            raise ValueError("duplicate task IDs")
        active, done = set(), set()
        def visit(task_id: UUID):
            if task_id in active:
                raise ValueError("dependency cycle")
            if task_id in done:
                return
            active.add(task_id)
            for dep in by_id[task_id].dependency_ids:
                if dep not in by_id or by_id[dep].day > by_id[task_id].day:
                    raise ValueError("dependency missing or scheduled after task")
                visit(dep)
            active.remove(task_id)
            done.add(task_id)
        for tid in by_id:
            visit(tid)
        return self


class CopyVariant(StrictModel):
    name: ShortText
    hook: Text
    body: Text
    call_to_action: Text
    language: Language
    status: Literal["draft"] = "draft"


class GenerationPrompt(StrictModel):
    medium: Literal["image", "video", "text"]
    prompt: Text
    constraints: list[Text]
    prohibited_claims: list[Text]


class CreativeBrief(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    brand_id: UUID
    plan_id: UUID
    task_id: UUID
    objective: Text
    audience: Text
    channel: ShortText
    format: Literal["reel", "carousel", "static", "video", "linkedin_post", "landing_page", "seo_article", "email"]
    key_message: Text
    evidence: list[Claim]
    hooks: list[Text]
    copy_variants: list[CopyVariant]
    visual_direction: Text
    generation_prompts: list[GenerationPrompt]
    test_hypothesis: Text
    regulated_category_flags: list[ShortText]
    disclosure_required: bool
    upfront_disclosure: Literal["#Ad", "#Sponsored", "#Collaboration", "#Partnership"] | None
    compliance_checks: list[Text]
    assumptions: list[Claim]
    missing_inputs: list[Gap]
    status: Literal["draft"] = "draft"

    @model_validator(mode="after")
    def disclosure(self) -> Self:
        if self.disclosure_required and not self.upfront_disclosure:
            raise ValueError("paid partnership content needs upfront disclosure")
        return self


class ContextReference(StrictModel):
    entity: ShortText
    entity_id: UUID
    version: Annotated[int, Field(ge=1)]
    fields: list[ShortText]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class TokenUsage(StrictModel):
    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]
    token_budget: Annotated[int, Field(ge=1)]


class PromptRun(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: UUID
    brand_id: UUID
    workflow_id: UUID
    task_type: Literal["brand_research", "competitor_research", "audience_research", "market_research", "social_listening", "content_gap", "strategy", "strategy_critique", "thirty_day_plan", "creative_brief", "ad_copy", "seo_plan", "email_sequence", "performance", "weekly_recommendation", "client_report", "client_presentation"]
    template_id: UUID
    template_version: Annotated[int, Field(ge=1)]
    provider: ShortText
    model: ShortText
    input_references: list[ContextReference]
    compiled_prompt_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    output_schema: ShortText
    output_artifact_id: UUID | None
    usage: TokenUsage
    cost: Money | None
    latency_ms: Annotated[int, Field(ge=0)]
    attempt: Annotated[int, Field(ge=1, le=2)]
    status: Literal["queued", "running", "valid", "invalid", "failed", "needs_human_review"]
    error_code: ShortText | None
    started_at: datetime
    finished_at: datetime | None


class ApprovalRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    approval_id: UUID
    brand_id: UUID
    workflow_id: UUID
    artifact_id: UUID
    artifact_version: Annotated[int, Field(ge=1)]
    artifact_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    requested_by: UUID
    assigned_to: UUID
    decision: Literal["pending", "approved", "revision_requested", "rejected"]
    decided_by: UUID | None
    decided_at: datetime | None
    reason: Text | None
    selected_strategy_option_id: UUID | None
    approved_kpi_names: list[ShortText]

    @model_validator(mode="after")
    def actor(self) -> Self:
        if self.decision == "pending":
            if self.decided_at or self.decided_by:
                raise ValueError("pending approval has no decision actor")
        elif self.decided_by != self.assigned_to or not self.decided_at:
            raise ValueError("decision requires assigned human approver and timestamp")
        return self


class AuditLog(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    audit_id: UUID
    organization_id: UUID
    brand_id: UUID | None
    actor_id: UUID
    action: ShortText
    entity_type: ShortText
    entity_id: UUID | None
    request_id: UUID
    input_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None
    output_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None
    source_ids: list[UUID]
    error_code: ShortText | None
    attempt: Annotated[int, Field(ge=1)]
    occurred_at: datetime
    previous_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None
    entry_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def validate_source_references(model: BaseModel, allowed: set[UUID]) -> None:
    def walk(value):
        if isinstance(value, Claim):
            if not set(value.source_ids).issubset(allowed):
                raise ValueError("claim references a source outside this artifact")
        if isinstance(value, BaseModel):
            for name in type(value).model_fields:
                walk(getattr(value, name))
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(model)


SCHEMA_MODELS = {"brand-profile": BrandProfile, "research-dossier": ResearchDossier,
    "strategy-options": StrategyOptions, "red-team-review": RedTeamReview,
    "30-day-plan": ThirtyDayPlan, "creative-brief": CreativeBrief,
    "prompt-run": PromptRun, "approval-record": ApprovalRecord, "audit-log": AuditLog}


class Signup(StrictModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=12, max_length=128)]
    full_name: ShortText
    organization_name: ShortText


class Login(StrictModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=128)]


class RefreshRequest(StrictModel):
    refresh_token: Annotated[str, Field(min_length=32, max_length=200)]


class TokenPair(StrictModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class PresenceLink(StrictModel):
    url: HttpUrl
    platform: Literal["website", "instagram", "linkedin", "facebook", "youtube", "other"] = "other"


class BrandCreate(StrictModel):
    name: ShortText
    website_url: HttpUrl | None = None
    online_presence: Annotated[list[PresenceLink], Field(max_length=12)] = Field(default_factory=list)
    category: ShortText | None = None
    objectives: Annotated[list[ShortText], Field(max_length=8)] = Field(default_factory=list)

    @model_validator(mode="after")
    def presence_required(self):
        if not self.website_url and not self.online_presence:
            raise ValueError("Add a public website or online presence URL")
        return self
    business_description: Text | None = None
    primary_objective: Text | None = None
    target_geographies: list[ShortText] = Field(default_factory=lambda: ["India"])
    languages: list[Language] = Field(default_factory=lambda: ["en"])


class OnboardingInput(StrictModel):
    brand_name: ShortText | None = None
    business_description: Text | None = None
    business_model: Text | None = None
    products_services: list[Text] = Field(default_factory=list)
    target_geographies: list[ShortText] = Field(default_factory=list)
    audience_description: Text | None = None
    primary_objective: Text | None = None
    budget_range: MoneyRange | None = None
    existing_asset_ids: list[UUID] = Field(default_factory=list)
    past_performance: Text | None = None
    competitor_names: list[ShortText] = Field(default_factory=list)
    constraints: list[Text] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=lambda: ["en"])


class OnboardingSave(StrictModel):
    expected_version: Annotated[int, Field(ge=0)]
    answers: OnboardingInput


class VersionRequest(StrictModel):
    expected_version: Annotated[int, Field(ge=0)]


class InviteCreate(StrictModel):
    email: EmailStr
    role: Literal["admin", "team", "client"]
    brand_id: UUID
    scopes: list[Literal["brand:read", "internal:read", "onboarding:read", "onboarding:write", "crawl:run", "profile:write", "artifact:write", "workflow:run", "assets:write", "research:run", "planning:run"]] = Field(default_factory=list)


class InviteAccept(StrictModel):
    token: Annotated[str, Field(min_length=32, max_length=200)]
    full_name: ShortText
    password: Annotated[str, Field(min_length=12, max_length=128)]


class CrawlRequest(StrictModel):
    max_pages: Annotated[int, Field(ge=1, le=8)] = 5


class WorkflowEvent(StrictModel):
    expected_version: Annotated[int, Field(ge=1)]
    event: Literal["retry", "cancel"]


class ErrorResponse(StrictModel):
    detail: str


class FounderActivation(StrictModel):
    email: EmailStr
    full_name: ShortText
    password: Annotated[str, Field(min_length=12, max_length=128)]
    token: Annotated[str, Field(min_length=32, max_length=200)]
