"""Conservative extraction: observations only, with explicit unanswered questions."""
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.models import BrandProfile, Citation, Claim, Gap, ObservedText, OnboardingQuestion, Product

from app.models import BrandProfileV11 as BrandProfile

QUESTIONS = {
    "business_description": ("What does the business sell, and to whom?", "long_text", True),
    "business_model": ("How does the business earn revenue?", "long_text", False),
    "audience_description": ("Who is the primary customer, including geography and buying context?", "long_text", True),
    "primary_objective": ("What specific business outcome is the priority?", "long_text", True),
    "budget_range": ("What monthly marketing budget range is available in INR?", "money_range", True),
    "products_services": ("Which products or services should the strategy prioritise?", "list", False),
    "constraints": ("What operational, legal, production or brand constraints apply? State none if applicable.", "list", False),
}


def gaps(profile: BrandProfile) -> BrandProfile:
    missing, questions = [], []
    for field, (question, kind, critical) in QUESTIONS.items():
        if not getattr(profile, "objectives" if field == "primary_objective" else field):
            reason = "Not established by the supplied inputs or captured website evidence."
            missing.append(Gap(field=field, question=question, critical=critical, reason=reason))
            questions.append(OnboardingQuestion(field=field, label=question, required=critical, input_type=kind, reason=reason))
    return profile.model_copy(update={"missing_inputs": missing, "onboarding_questions": questions})


def observed(value: str, source_id: UUID, label: str) -> ObservedText:
    return ObservedText(value=value, evidence=Claim(claim_id=uuid4(), classification="fact",
        statement=label + ": " + value, source_ids=[source_id], confidence=1.0,
        rationale="Verbatim supplied input or captured page metadata; the underlying marketing claim is not independently verified.",
        geographic_scope="unknown"))


def extract_profile(brand_id: UUID, version: int, intake: dict, pages: list[tuple], input_source_id: UUID) -> BrandProfile:
    """pages is [(CrawledPage, source UUID)]. Never infer budgets, pricing or audiences."""
    now = datetime.now(timezone.utc)
    sources = [Citation(source_id=input_source_id, url=intake.get("website_url") or intake["online_presence"][0]["url"], publisher="Founder supplied intake",
                        published_at=None, accessed_at=now, geographic_scope="unknown", applicability_to_india=None,
                        access_limitations=["Self reported input; not independent verification."])]
    description = observed(intake["business_description"], input_source_id, "Founder reports") if intake.get("business_description") else None
    for page, source_id in pages:
        sources.append(Citation(source_id=source_id, url=page.url, publisher=page.title, published_at=None,
            accessed_at=now, geographic_scope="unknown", applicability_to_india=None,
            access_limitations=["Public HTML only. No scripts, authentication or independent verification."]))
        if description is None and page.description:
            description = observed(page.description, source_id, "Website description states")
    profile = BrandProfile(schema_version="1.1" if not intake.get("website_url") else "1.0", brand_id=brand_id, version=version,
        name=observed(intake["name"], input_source_id, "Founder reports the brand name"), website_url=intake.get("website_url"),
        business_description=description, business_model=None, positioning=None, products_services=[],
        target_geographies=intake.get("target_geographies", []),
        geography_evidence=[observed(", ".join(intake["target_geographies"]), input_source_id, "Configured research geographies").evidence] if intake.get("target_geographies") else [],
        audience_description=None, objectives=intake.get("objectives") or ([intake["primary_objective"]] if intake.get("primary_objective") else []),
        budget_range=None, pricing_margin_notes=None, constraints=[], languages=intake.get("languages", ["en"]),
        competitors=[], conversion_paths=[], proof_points=[], sources=sources, missing_inputs=[], onboarding_questions=[],
        data_quality_warnings=["Crawl coverage is incomplete. Website claims are not independent proof.",
                               "Pricing, margins, audiences and commercial outcomes remain unknown unless explicitly supplied."])
    return gaps(profile)


def merge_onboarding(profile: BrandProfile, answers: dict, source_id: UUID) -> BrandProfile:
    """Preserve provenance and do not turn missing answers into inferred facts."""
    data = profile.model_dump(mode="python")
    source = Citation(source_id=source_id, url=profile.website_url or profile.sources[0].url, publisher="Client onboarding response",
        published_at=None, accessed_at=datetime.now(timezone.utc), geographic_scope="unknown",
        applicability_to_india=None, access_limitations=["Self reported client input."])
    data["sources"] = [*profile.sources, source]
    for key in ("business_description", "business_model", "audience_description"):
        if answers.get(key):
            data[key] = observed(answers[key], source_id, "Onboarding respondent reports")
    if answers.get("brand_name"):
        data["name"] = observed(answers["brand_name"], source_id, "Onboarding respondent reports")
    if answers.get("primary_objective"):
        data["objectives"] = [answers["primary_objective"]]
    for key in ("budget_range", "constraints", "languages", "target_geographies"):
        if answers.get(key):
            data[key] = answers[key]
    if answers.get("target_geographies"):
        data["geography_evidence"] = [observed(", ".join(answers["target_geographies"]), source_id, "Onboarding target geographies").evidence]
    if answers.get("competitor_names"):
        data["competitors"] = answers["competitor_names"]
    if answers.get("products_services"):
        data["products_services"] = [Product(name=observed(value, source_id, "Onboarding reports a product or service"), description=None, price=None, price_evidence=None) for value in answers["products_services"]]
    return gaps(BrandProfile.model_validate(data))
