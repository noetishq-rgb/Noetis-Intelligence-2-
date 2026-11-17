"""Provider-neutral request boundary; no model tools can execute external work."""
from typing import Protocol
from app.models import StrictModel, ShortText, ContextReference


class ModelRequest(StrictModel):
    task_type: ShortText
    template_version: int
    context: list[ContextReference]
    system: str
    context_json: str
    output_schema_json: str
    output_schema_name: ShortText
    maximum_input_tokens: int
    maximum_output_tokens: int


class ModelResult(StrictModel):
    provider: ShortText
    model: ShortText
    output_json: str
    input_tokens: int
    output_tokens: int


class ModelProvider(Protocol):
    def count_tokens(self, request: ModelRequest) -> int: ...
    def generate(self, request: ModelRequest) -> ModelResult: ...
