"""Structural and semantic validation boundary for future model outputs."""
from uuid import UUID
from jsonschema import Draft202012Validator, FormatChecker
from app.models import SCHEMA_MODELS, RedTeamReview, validate_source_references


def validate_output(schema_name: str, payload: dict, brand_id: UUID,
                    allowed_source_ids: set[UUID] | None = None,
                    strategy_option_ids: set[UUID] | None = None):
    model = SCHEMA_MODELS[schema_name]
    Draft202012Validator(model.model_json_schema(), format_checker=FormatChecker()).validate(payload)
    result = model.model_validate(payload)
    if result.brand_id != brand_id:
        raise ValueError("output brand does not match the authenticated workflow")
    if allowed_source_ids is not None:
        validate_source_references(result, allowed_source_ids)
        if hasattr(result, "sources") and not {source.source_id for source in result.sources} <= allowed_source_ids:
            raise ValueError("unregistered citation")
    if isinstance(result, RedTeamReview) and strategy_option_ids is not None:
        ids = [review.option_id for review in result.reviews]
        if len(ids) != len(set(ids)) or set(ids) != strategy_option_ids:
            raise ValueError("red team review must cover every option exactly once")
    return result
