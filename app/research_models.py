"""Phase 2 contracts. Dossier 1.1 preserves the original 1.0 contract."""
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID
from pydantic import Field, HttpUrl, model_validator
from app.models import (StrictModel, ShortText, Text, ResearchDossier, ContextReference, Citation, Confidence)

Area = Literal["brand", "market", "audience", "competitors", "social", "reviews", "search", "seasonality", "creative", "regulatory"]
AREAS = ["brand", "market", "audience", "competitors", "social", "reviews", "search", "seasonality", "creative", "regulatory"]


class SourceSeed(StrictModel):
    url: HttpUrl
    kind: Literal["public_web", "social", "review", "search"] = "public_web"
    area: Area = "market"
    geographic_scope: Literal["india", "global", "unknown"] = "unknown"
    applicability_to_india: Text | None = None

    @model_validator(mode="after")
    def global_reason(self):
        if self.geographic_scope == "global" and not self.applicability_to_india:
            raise ValueError("global context requires India applicability")
        return self


class ResearchBrief(StrictModel):
    question: Annotated[str, Field(max_length=1000)] = ''
    tone_of_voice: Annotated[str, Field(max_length=400)] = ''
    guidelines: Annotated[str, Field(max_length=4000)] = ''


class ResearchStart(StrictModel):
    brief: ResearchBrief | None = None
    task_type: Literal["brand_research", "competitor_research", "audience_research", "market_research", "social_listening", "content_gap"] = "brand_research"
    expected_workflow_version: Annotated[int, Field(ge=1)]
    sources: Annotated[list[SourceSeed], Field(max_length=8)] = Field(default_factory=list)
    search_queries: Annotated[list[Annotated[str, Field(min_length=3, max_length=200)]], Field(max_length=4)] = Field(default_factory=list)
    areas: Annotated[list[Area], Field(min_length=1, max_length=10)] = Field(default_factory=lambda: list(AREAS))
    acknowledge_uncertain_charge: bool = False

    @model_validator(mode="after")
    def inputs(self):
        if not self.sources and not self.search_queries:
            raise ValueError("supply source URLs or public discovery queries")
        if len(self.areas) != len(set(self.areas)):
            raise ValueError("duplicate research areas")
        return self


class ClaimSupport(StrictModel):
    claim_id: UUID
    source_id: UUID
    quote: Annotated[str, Field(min_length=12, max_length=1200)]


class Coverage(StrictModel):
    area: Area
    status: Literal["supported", "limited", "unavailable"]
    finding_ids: list[UUID]
    limitations: list[Text]


class Contradiction(StrictModel):
    conflict_id: UUID
    claim_ids: Annotated[list[UUID], Field(min_length=2)]
    explanation: Text
    critical: bool


class ResearchDossierV11(ResearchDossier):
    schema_version: Literal["1.1"] = "1.1"
    claim_support: list[ClaimSupport]
    coverage: Annotated[list[Coverage], Field(min_length=10, max_length=10)]
    conflict_pairs: list[Contradiction]

    @model_validator(mode="after")
    def area_coverage(self):
        if {c.area for c in self.coverage} != set(AREAS):
            raise ValueError("each research area needs exactly one coverage entry")
        for item in self.coverage:
            if item.status == "supported" and not item.finding_ids:
                raise ValueError("supported coverage requires findings")
            if item.status == "unavailable" and item.finding_ids:
                raise ValueError("unavailable coverage cannot imply findings")
            if item.status != "supported" and not item.limitations:
                raise ValueError("limited coverage requires an explanation")
            if item.status != "supported" and not any(g.field == item.area for g in self.missing_inputs):
                raise ValueError("limited coverage requires an explicit gap")
        return self


class GapResolution(StrictModel):
    field: ShortText
    answer: Text
    kind: Literal["client_input", "evidence"]
    source_ids: list[UUID]
    claim_ids: list[UUID] = Field(default_factory=list)
    rationale: Text | None = None


class ConflictResolution(StrictModel):
    conflict_id: UUID
    selected_claim_id: UUID
    rationale: Text
    source_ids: Annotated[list[UUID], Field(min_length=1)]


class ClaimAssessment(StrictModel):
    claim_id: UUID
    verdict: Literal["supported", "unsupported", "uncertain"]
    source_ids: Annotated[list[UUID], Field(min_length=1)]
    rationale: Text


class SemanticReview(StrictModel):
    source_quality_checked: bool
    geography_checked: bool
    contradictions_checked: bool
    coverage_checked: bool
    claim_assessments: list[ClaimAssessment]
    unresolved_issues: list[Text] = Field(default_factory=list)


class ResearchReview(StrictModel):
    expected_workflow_version: Annotated[int, Field(ge=1)]
    artifact_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    decision: Literal["approved", "revision_requested"]
    reason: Text
    gap_resolutions: list[GapResolution] = Field(default_factory=list)
    conflict_resolutions: list[ConflictResolution] = Field(default_factory=list)
    semantic_review: SemanticReview | None = None


class EvidenceWarning(StrictModel):
    source_id: UUID
    code: Literal["publication_date_unknown", "publication_older_than_365_days", "geography_unknown", "global_context_only"]


class ResearchValidationReport(StrictModel):
    validator_version: Literal["3.0"] = "3.0"
    checked_at: datetime
    schema_status: Literal["passed", "failed"]
    provenance_status: Literal["passed", "failed", "not_run"]
    semantic_status: Literal["not_assessed"] = "not_assessed"
    issue_codes: list[ShortText]
    evidence_warnings: list[EvidenceWarning]
    output_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class ResearchResume(StrictModel):
    expected_workflow_version: Annotated[int, Field(ge=1)]
    acknowledge_uncertain_charge: bool = False


class ResearchAccepted(StrictModel):
    brand_id: UUID
    workflow_id: UUID
    job_id: UUID
    state: str


class ResearchRunView(StrictModel):
    budget_totals_scope: Literal["workflow"] = "workflow"
    job_id: UUID
    brand_id: UUID
    status: str
    attempts: int
    last_error: str | None
    source_count: int
    model_calls: int
    tokens_used: int
    tokens_reserved: int
    cost_used_paise: int
    cost_reserved_paise: int
    uncertain_dispatches: int
    pricing_complete: bool
    currency: Literal["INR"] = "INR"
    dossier_id: UUID | None
    source_limitations: list[Text]
    latest_validation: ResearchValidationReport | None = None


class ResearchReviewRecord(StrictModel):
    id: UUID
    reviewer_id: UUID
    reviewed_at: datetime
    decision: Literal["approved", "revision_requested"]
    reason: str
    gap_resolutions: list[GapResolution]
    conflict_resolutions: list[ConflictResolution]
    semantic_review: SemanticReview | None = None


class DossierView(StrictModel):
    id: UUID
    artifact_id: UUID
    artifact_version: int
    artifact_sha256: str
    profile_artifact_id: UUID
    content: ResearchDossierV11
    review_status: Literal["pending", "approved", "revision_requested"]
    review_record: ResearchReviewRecord | None = None
    semantic_review_required: Literal[True] = True
    semantic_review_status: Literal["pending", "approved", "revision_requested", "historical_unassessed"] = "pending"
    validation_report: ResearchValidationReport | None = None


class PromptCompile(StrictModel):
    evidence_ids: Annotated[list[UUID], Field(min_length=1, max_length=8)]
    task_type: Literal["brand_research", "competitor_research", "audience_research", "market_research", "social_listening", "content_gap"] = "brand_research"
    areas: Annotated[list[Area],Field(min_length=1,max_length=10)] = Field(default_factory=lambda: list(AREAS))


class CompiledPromptView(StrictModel):
    template_version: int
    system: str
    context_json: str
    output_schema_json: str
    prompt_sha256: str
    references: list[ContextReference]
    estimated_input_tokens: int
    token_count_is_estimate: Literal[True] = True


class EvidenceView(StrictModel):
    id: UUID
    source: Citation
    area: Area
    kind: str
    excerpt: str
    captured_snapshot_sha256: str
    confidence_cap: Confidence
