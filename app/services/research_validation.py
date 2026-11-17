"""Deterministic provenance checks are separate from human semantic decisions."""
import hashlib
import json
from datetime import date, datetime, timezone
from uuid import UUID
from pydantic import BaseModel, ValidationError
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as SchemaError
from app.models import Claim, Citation
from app.research_models import AREAS, ResearchDossierV11, ResearchValidationReport, EvidenceWarning


class ResearchValidationError(ValueError):
    def __init__(self, code, message, *, stage="provenance"):
        super().__init__(message)
        self.code = code
        self.stage = stage


def fail(code, message):
    raise ResearchValidationError(code, message)


def claims_in(value):
    if isinstance(value, Claim):
        yield value
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from claims_in(getattr(value, name))
    elif isinstance(value, list):
        for child in value:
            yield from claims_in(child)


def area_findings(dossier):
    return {"brand": dossier.brand_findings, "market": dossier.category_market,
        "audience": dossier.audiences, "competitors": dossier.competitors, "social": dossier.social_listening,
        "reviews": dossier.reputation_reviews, "search": dossier.search_opportunities,
        "seasonality": dossier.demand_seasonality, "creative": dossier.creative_content_gaps,
        "regulatory": dossier.regulatory_cultural_risks}


def normal(text):
    return " ".join(text.split())


def validate_dossier(payload, brand_id, profile_version, evidence_rows, expected_as_of=None):
    try:
        Draft202012Validator(ResearchDossierV11.model_json_schema(), format_checker=FormatChecker()).validate(payload)
        dossier = ResearchDossierV11.model_validate(payload)
    except (SchemaError, ValidationError) as exc:
        raise ResearchValidationError("research_contract_invalid", "Research output does not match the dossier contract", stage="schema") from exc
    if dossier.brand_id != brand_id or dossier.profile_version != profile_version:
        fail("research_parent_mismatch", "Stale or cross-brand research output")
    if expected_as_of and dossier.research_as_of.isoformat() != expected_as_of:
        fail("research_cutoff_mismatch", "Research cutoff must match captured evidence")
    registry = {UUID(row["citation"]["source_id"]): row for row in evidence_rows}
    ids = [source.source_id for source in dossier.sources]
    if len(ids) != len(set(ids)) or not set(ids) <= set(registry):
        fail("research_source_invalid", "Duplicate or unregistered source")
    for source in dossier.sources:
        if source != Citation.model_validate(registry[source.source_id]["citation"]):
            fail("research_source_metadata_mismatch", "Citation metadata differs from the captured source")
    claims = list(claims_in(dossier))
    by_id = {claim.claim_id: claim for claim in claims}
    if len(claims) != len(by_id):
        fail("research_claim_duplicate", "Duplicate claim IDs")
    gap_names = [g.field.casefold() for g in dossier.missing_inputs]
    if len(gap_names) != len(set(gap_names)):
        fail("research_gap_duplicate", "Duplicate research gap fields")
    supports, seen_supports = {}, set()
    for support in dossier.claim_support:
        claim = by_id.get(support.claim_id)
        row = registry.get(support.source_id)
        if not claim or not row or support.source_id not in claim.source_ids:
            fail("research_support_reference_invalid", "Support references a different claim or source")
        key = (support.claim_id, support.source_id, normal(support.quote))
        if key in seen_supports:
            fail("research_support_duplicate", "Duplicate claim support")
        seen_supports.add(key)
        if normal(support.quote) not in normal(row["extracted_text"][:2400]):
            fail("research_quote_missing", "Supporting quote is absent from the supplied evidence")
        supports.setdefault(claim.claim_id, []).append(support)
    factual = {c.claim_id: c for c in claims if c.classification in {"fact", "inference"}}
    for claim in claims:
        if len(claim.source_ids) != len(set(claim.source_ids)):
            fail("research_claim_source_duplicate", "Duplicate source reference on a claim")
        if claim.claim_id in factual:
            supported_sources = {s.source_id for s in supports.get(claim.claim_id, [])}
            if supported_sources != set(claim.source_ids):
                fail("research_citation_unsupported", "Every factual citation requires its own captured supporting quotation")
            caps = [float(registry[sid]["confidence_cap"]) for sid in supported_sources]
            if claim.confidence > max(caps):
                fail("research_confidence_exceeded", "Confidence exceeds available evidence")
            if claim.geographic_scope == "india" and not any(registry[sid]["citation"]["geographic_scope"] == "india" for sid in supported_sources):
                fail("research_india_evidence_missing", "India-specific finding requires India-specific evidence")
    if not factual:
        fail("research_findings_missing", "Research requires at least one evidence-supported finding")
    findings = area_findings(dossier)
    for coverage in dossier.coverage:
        area_claims = {c.claim_id: c for c in claims_in(findings[coverage.area])}
        listed = set(coverage.finding_ids)
        if len(listed) != len(coverage.finding_ids):
            fail("research_coverage_duplicate", "Duplicate coverage finding reference")
        if not listed <= set(area_claims):
            fail("research_coverage_reference_invalid", "Coverage references a missing or different-area finding")
        evidence_backed = set(area_claims) & set(factual)
        if listed - evidence_backed:
            fail("research_coverage_unsubstantiated", "Assumptions and gaps cannot count as evidence-supported coverage")
        if listed != evidence_backed:
            fail("research_coverage_incomplete", "Coverage must account for every factual or inferred finding in its area")
    conflict_ids = [c.conflict_id for c in dossier.conflict_pairs]
    if len(conflict_ids) != len(set(conflict_ids)):
        fail("research_conflict_duplicate", "Duplicate contradiction IDs")
    for conflict in dossier.conflict_pairs:
        if len(set(conflict.claim_ids)) != len(conflict.claim_ids) or not set(conflict.claim_ids) <= set(by_id):
            fail("research_conflict_reference_invalid", "Contradiction must reference distinct existing claims")
    if dossier.contradictions and not dossier.conflict_pairs:
        fail("research_conflict_links_missing", "Reported contradictions require explicit linked claim pairs")
    return dossier


def evidence_warnings(evidence_rows, as_of):
    warnings = []
    cutoff = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    for row in evidence_rows:
        citation = Citation.model_validate(row["citation"])
        codes = []
        if citation.published_at is None:
            codes.append("publication_date_unknown")
        elif cutoff and (cutoff - citation.published_at).days > 365:
            codes.append("publication_older_than_365_days")
        if citation.geographic_scope == "unknown":
            codes.append("geography_unknown")
        elif citation.geographic_scope == "global":
            codes.append("global_context_only")
        warnings.extend(EvidenceWarning(source_id=citation.source_id, code=code) for code in codes)
    return warnings


def evaluate_output(raw, brand_id, profile_version, evidence_rows, expected_as_of):
    """Return sanitized durable diagnostics; never label semantic truth as validated."""
    dossier, error = None, None
    try:
        if len(raw) > 200000:
            raise ResearchValidationError("research_output_too_large", "Research output exceeds the limit", stage="schema")
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ResearchValidationError("research_json_invalid", "Research output is not valid JSON", stage="schema") from exc
        dossier = validate_dossier(payload, brand_id, profile_version, evidence_rows, expected_as_of)
    except ResearchValidationError as exc:
        error = exc
    report = ResearchValidationReport(checked_at=datetime.now(timezone.utc),
        schema_status="failed" if error and error.stage == "schema" else "passed",
        provenance_status=("not_run" if error.stage == "schema" else "failed") if error else "passed",
        issue_codes=[error.code] if error else [], evidence_warnings=evidence_warnings(evidence_rows, expected_as_of),
        output_sha256=hashlib.sha256(raw.encode()).hexdigest())
    return dossier, report


def validate_review(dossier, review):
    """Require source-linked human assessments, without pretending to automate meaning."""
    sources = {s.source_id for s in dossier.sources}
    claims = {c.claim_id: c for c in claims_in(dossier)}
    factual = {cid: c for cid, c in claims.items() if c.classification in {"fact", "inference"}}
    semantic = review.semantic_review
    assessments = {a.claim_id: a for a in semantic.claim_assessments} if semantic else {}
    if semantic:
        if len(assessments) != len(semantic.claim_assessments) or not set(assessments) <= set(factual):
            fail("review_claim_reference_invalid", "Human assessments must reference unique factual/inferred claims")
        for assessment in assessments.values():
            if len(assessment.source_ids) != len(set(assessment.source_ids)) or set(assessment.source_ids) != set(factual[assessment.claim_id].source_ids):
                fail("review_claim_source_mismatch", "Human assessment must cover every cited source for its claim")
    gap_map = {g.field: g for g in dossier.missing_inputs}
    resolutions = {r.field: r for r in review.gap_resolutions}
    if len(resolutions) != len(review.gap_resolutions) or not set(resolutions) <= set(gap_map):
        fail("review_gap_reference_invalid", "Gap resolutions must reference unique existing gaps")
    client_fields = {"budget_range", "audience_description", "objectives", "primary_objective", "constraints", "icp", "primary_icp"}
    findings = area_findings(dossier)
    for answer in resolutions.values():
        if len(answer.source_ids) != len(set(answer.source_ids)) or len(answer.claim_ids) != len(set(answer.claim_ids)):
            fail("review_gap_reference_duplicate", "Duplicate gap evidence reference")
        if answer.kind == "client_input":
            if answer.field.casefold() not in client_fields or answer.source_ids or answer.claim_ids:
                fail("review_gap_attestation_invalid", "Only intake gaps can be resolved by client input without research evidence")
        else:
            if not answer.rationale or not answer.source_ids or not answer.claim_ids or not set(answer.claim_ids) <= set(factual):
                fail("review_gap_evidence_missing", "Research gap resolutions need supported claim IDs, source IDs and a rationale")
            linked_sources = {sid for cid in answer.claim_ids for sid in factual[cid].source_ids}
            if not set(answer.source_ids) <= sources & linked_sources:
                fail("review_gap_source_unrelated", "Gap evidence must support its referenced findings")
            if answer.field in AREAS and not set(answer.claim_ids) <= {c.claim_id for c in claims_in(findings[answer.field])}:
                fail("review_gap_area_mismatch", "Gap evidence must address the same research area; new evidence requires revision")
            if review.decision == "approved" and any(cid not in assessments or assessments[cid].verdict != "supported" for cid in answer.claim_ids):
                fail("review_gap_not_supported", "Gap evidence requires a supported human assessment")
    conflicts = {c.conflict_id: c for c in dossier.conflict_pairs}
    decisions = {r.conflict_id: r for r in review.conflict_resolutions}
    if len(decisions) != len(review.conflict_resolutions) or not set(decisions) <= set(conflicts):
        fail("review_conflict_reference_invalid", "Contradiction resolutions must reference unique existing conflicts")
    for decision in decisions.values():
        conflict = conflicts[decision.conflict_id]
        if decision.selected_claim_id not in conflict.claim_ids:
            fail("review_conflict_claim_invalid", "Contradiction decision must select an involved claim")
        if len(set(decision.source_ids)) != len(decision.source_ids) or not set(decision.source_ids) <= set(claims[decision.selected_claim_id].source_ids) & sources:
            fail("review_conflict_source_unrelated", "Every contradiction decision source must support the selected finding")
    if review.decision == "revision_requested":
        return
    if not semantic or not all([semantic.source_quality_checked, semantic.geography_checked,
        semantic.contradictions_checked, semantic.coverage_checked]):
        fail("review_semantic_required", "Approval requires an explicit human assessment of source quality, geography, contradictions and coverage")
    if set(assessments) != set(factual):
        fail("review_claims_incomplete", "Approval requires a human assessment of every factual or inferred claim")
    if semantic.unresolved_issues or any(a.verdict != "supported" for a in assessments.values()):
        fail("review_semantic_unresolved", "Unsupported or uncertain findings and unresolved issues require revision")
    for gap in dossier.missing_inputs:
        blocking = gap.critical or any(c.area == gap.field and c.area in {"market", "audience"} and c.status == "unavailable" for c in dossier.coverage)
        if blocking and gap.field not in resolutions:
            fail("review_gap_unresolved", "Critical research gaps must be resolved before approval")
    if any(c.critical and c.conflict_id not in decisions for c in dossier.conflict_pairs):
        fail("review_conflict_unresolved", "Critical contradictions require an explicit human decision")
