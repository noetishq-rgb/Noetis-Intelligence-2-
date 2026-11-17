from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID
from pydantic import Field, EmailStr
from app.models import StrictModel, ShortText, OnboardingInput, OnboardingQuestion, BrandProfile


class UserView(StrictModel):
    id: UUID
    email: EmailStr
    full_name: str


class OrganizationView(StrictModel):
    id: UUID
    name: str
    role: str
    currency: Literal["INR"]


class BrandView(StrictModel):
    id: UUID
    organization_id: UUID
    name: str
    website_url: str | None
    status: str
    created_at: datetime


class BrandCreated(StrictModel):
    brand: BrandView
    workflow_id: UUID
    crawl_job_id: UUID


class InvitationCreated(StrictModel):
    invitation_id: UUID
    invitation_token: str
    expires_at: datetime
    delivery: Literal["manual"] = "manual"


class AcceptInvitation(StrictModel):
    invitation_token: Annotated[str, Field(min_length=32, max_length=200)]
    full_name: ShortText
    password: Annotated[str, Field(min_length=12, max_length=128)]


class AcceptExistingInvitation(StrictModel):
    invitation_token: Annotated[str, Field(min_length=32, max_length=200)]


class AcceptedInvitation(StrictModel):
    brand_id: UUID


class Questionnaire(StrictModel):
    brand_id: UUID
    profile_version: int
    questions: list[OnboardingQuestion]


class OnboardingView(StrictModel):
    id: UUID | None
    brand_id: UUID
    respondent_id: UUID
    version: int
    status: Literal["empty", "draft", "submitted"]
    answers: OnboardingInput
    submitted_at: datetime | None


class ProfileConfirm(StrictModel):
    expected_profile_version: Annotated[int, Field(ge=1)]
    onboarding_response_id: UUID


class JobView(StrictModel):
    id: UUID
    brand_id: UUID
    workflow_id: UUID
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None
    created_at: datetime


class WorkflowView(StrictModel):
    id: UUID
    brand_id: UUID
    state: str
    version: int
    validation_failures: int
    agent_calls: int
    max_agent_calls: int
    tokens_used: int
    token_budget: int
    approved_profile_id: UUID | None
    approved_strategy_id: UUID | None
    approved_research_id: UUID | None = None
    selected_strategy_approval_id: UUID | None = None
    approved_plan_id: UUID | None = None
    approved_creative_id: UUID | None = None
    last_error: str | None


class AuditView(StrictModel):
    id: UUID
    brand_id: UUID
    actor_id: UUID
    action: str
    entity_type: str
    entity_id: UUID | None
    input_hash: str | None
    output_hash: str | None
    error_code: str | None
    occurred_at: datetime
    previous_hash: str | None
    entry_hash: str


class StatusView(StrictModel):
    status: str
