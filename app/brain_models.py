"""Closed contracts for the Brand Brain adapter; original research models stay authoritative."""
from typing import Annotated, Literal
from uuid import UUID
from pydantic import Field, model_validator
from app.models import StrictModel, PresenceLink, ShortText, Citation

CHAPTERS={'business':'Business & Offerings','audience':'Audience Intelligence','positioning':'Brand Positioning','market':'Market Intelligence','presence':'Marketing Presence','objectives':'Business Objectives'}
Provenance=Literal['publicly_observed','ai_inferred','founder_provided','founder_confirmed','unverified']
Confidence=Literal['high','medium','low','unknown']
ChapterState=Literal['not_started','in_review','changes_requested','approved','needs_attention']

class PresenceDraft(StrictModel):
    url: Annotated[str,Field(max_length=2048)]
    platform: Literal["website", "instagram", "linkedin", "facebook", "youtube", "other"] = "other"


class SetupDraft(StrictModel):
    brand_name: Annotated[str,Field(max_length=300)]=''
    online_presence: Annotated[list[PresenceDraft],Field(max_length=12)]=Field(default_factory=list)
    category: Annotated[str,Field(max_length=300)]=''
    niche: Annotated[str,Field(max_length=300)]=''
    objectives: Annotated[list[ShortText],Field(max_length=8)]=Field(default_factory=list)
    other_objective: Annotated[str,Field(max_length=600)]=''

    def require_complete(self):
        if not self.brand_name or not self.category or not self.online_presence or not self.objectives:
            raise ValueError('Provide a brand name, category, at least one public URL and one objective')
        urls=[str(link.url).rstrip('/') for link in self.online_presence]
        if len(urls)!=len(set(urls)):raise ValueError('Remove duplicate URLs')
        from app.services.crawl import validate_url
        for link in self.online_presence:validate_url(str(link.url))
        return self

class SessionSave(StrictModel):
    expected_version: Annotated[int,Field(ge=1)]
    draft: SetupDraft

class VersionAction(StrictModel):
    expected_version: Annotated[int,Field(ge=1)]

class FindingReview(StrictModel):
    expected_sequence: Annotated[int,Field(ge=0)]
    action: Literal['accept','edit','reject','request_revision','mark_unknown']
    text: Annotated[str,Field(min_length=1,max_length=10000)]|None=None
    reason: Annotated[str,Field(min_length=1,max_length=3000)]

    @model_validator(mode='after')
    def edit_text(self):
        if self.action=='edit' and not self.text:raise ValueError('An edited finding needs text')
        return self

class ThreadCreate(StrictModel):
    chapter: Literal['business','audience','positioning','market','presence','objectives']
    finding_id: UUID|None=None
    body: Annotated[str,Field(min_length=1,max_length=3000)]

class CommentCreate(StrictModel):
    body: Annotated[str,Field(min_length=1,max_length=3000)]

class ThreadAction(StrictModel):
    action: Literal['resolve','reopen']
    expected_event: UUID|None=None

class BrainConfirm(StrictModel):
    expected_review_sequence: Annotated[int,Field(ge=0)]
    approved_chapters: Annotated[list[Literal['business','audience','positioning','market','presence','objectives']],Field(min_length=1,max_length=6)]
    partial: bool=False
    acknowledge_limitations: bool

class Finding(StrictModel):
    id: UUID
    field: str
    original_text: str
    text: str
    provenance: Provenance
    confidence: Confidence
    source_ids: list[UUID]
    critical: bool=False
    unknown: bool=False

class Chapter(StrictModel):
    key: Literal['business','audience','positioning','market','presence','objectives']
    title: str
    summary: str
    findings: list[Finding]

class BrainDocument(StrictModel):
    schema_version: Literal['1.0']='1.0'
    brand_id: UUID
    brand_name: str
    chapters: Annotated[list[Chapter],Field(min_length=6,max_length=6)]
    sources: list[Citation]
    warnings: list[str]

ResearchJobState=Literal['created','queued','running','paused','awaiting_input','completed','failed','cancelled']
ResearchStageState=Literal['pending','running','completed','failed','skipped','needs_review']
class BrainStage(StrictModel):
    key: str
    title: str
    state: ResearchStageState
