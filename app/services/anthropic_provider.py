"""Messages API adapter. Exactly one POST per generation; automatic retries disabled."""
import json
import httpx
from app.config import settings
from app.services.model_provider import ModelRequest, ModelResult


class ProviderError(RuntimeError):
    def __init__(self, code: str, *, uncertain=False, retryable=False):
        super().__init__(code)
        self.code, self.uncertain, self.retryable = code, uncertain, retryable


class AnthropicProvider:
    name = "anthropic"
    def __init__(self, api_key: str, model: str, version: str, client=None):
        self.model = model
        self.version = version
        self.api_key = api_key
        self.client = client

    def payload(self, request: ModelRequest):
        tool = self.serialization_tool(request)
        return {"model": self.model, "system": request.system,
            "messages": [{"role": "user", "content": request.context_json}],
            "tools": [{"name": tool, "description": "Return the requested structured output. Serialization only; no action is executed.",
                       "input_schema": json.loads(request.output_schema_json)}],
            "tool_choice": {"type": "tool", "name": tool}}

    def serialization_tool(self, request):
        return 'submit_research_dossier' if request.output_schema_name.startswith('research-dossier') else 'submit_workflow_output'

    def post(self, path, payload, *, generation=False):
        headers = {"x-api-key": self.api_key, "anthropic-version": self.version, "content-type": "application/json"}
        client = self.client or httpx.Client(timeout=httpx.Timeout(60, connect=5), follow_redirects=False, trust_env=False)
        try:
            response = client.post("https://api.anthropic.com" + path, headers=headers, json=payload)
            if response.status_code != 200:
                raise ProviderError("model_http_" + str(response.status_code),
                    uncertain=generation and response.status_code >= 500,
                    retryable=not generation and response.status_code in {429, 500, 502, 503, 529})
            if len(response.content) > 2_000_000:
                raise ProviderError("model_response_too_large", uncertain=generation)
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("invalid provider envelope")
            return data
        except (httpx.TransportError, ValueError) as exc:
            raise ProviderError("model_outcome_unknown" if generation else "model_count_unavailable",
                                uncertain=generation, retryable=not generation) from exc
        finally:
            if self.client is None:
                client.close()

    def count_tokens(self, request: ModelRequest) -> int:
        result = self.post("/v1/messages/count_tokens", self.payload(request))
        value = result.get("input_tokens")
        if type(value) is not int or value < 1:
            raise ProviderError("invalid_token_count")
        return value

    def generate(self, request: ModelRequest) -> ModelResult:
        payload = self.payload(request)
        payload["max_tokens"] = request.maximum_output_tokens
        data = self.post("/v1/messages", payload, generation=True)
        usage = data.get("usage", {})
        if not isinstance(usage, dict):
            raise ProviderError("model_usage_unknown", uncertain=True)
        if any(type(usage.get(key)) is not int or usage[key] < 0 for key in ("input_tokens", "output_tokens")):
            raise ProviderError("model_usage_unknown", uncertain=True)
        if any(type(usage.get(key,0)) is not int or usage.get(key,0)<0 for key in ("cache_creation_input_tokens","cache_read_input_tokens")):
            raise ProviderError("model_usage_unknown", uncertain=True)
        content = data.get("content", [])
        tool_uses = [b for b in (content if isinstance(content,list) else []) if isinstance(b,dict) and b.get("type") == "tool_use"]
        blocks = [b for b in tool_uses if b.get("name") == self.serialization_tool(request)]
        output = blocks[0].get("input") if len(blocks) == len(tool_uses) == 1 and data.get("stop_reason") == "tool_use" else {"invalid_stop_reason": data.get("stop_reason")}
        return ModelResult(provider="anthropic", model=self.model, output_json=json.dumps(output),
                           input_tokens=usage["input_tokens"]+sum(usage.get(key,0) for key in ("cache_creation_input_tokens","cache_read_input_tokens")), output_tokens=usage["output_tokens"])


def get_model_provider():
    config = settings()
    if not config.allow_model_calls or config.model_provider != "anthropic" or not config.anthropic_api_key or not config.anthropic_model:
        raise ProviderError("model_not_configured")
    return AnthropicProvider(config.anthropic_api_key.get_secret_value(), config.anthropic_model, config.anthropic_version)


def get_planning_provider():
    if not settings().allow_planning_calls:
        raise ProviderError('planning_model_not_enabled')
    return get_model_provider()
