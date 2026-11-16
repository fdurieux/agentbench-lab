"""OpenAI-compatible chat adapter and offline protocol fixtures."""

from dataclasses import dataclass
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

TOOL = {
    "type": "function",
    "function": {
        "name": "read_spec",
        "description": "Read one normative specification fragment.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "enum": ["policy", "openapi"]}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
}
MAX_RESPONSE = 2_000_000

REFERENCE = """from datetime import date, timedelta
import re

def evaluate(request):
    def reject(reason):
        return {'status': 'REJECTED', 'reason': reason, 'settlement_date': None}

    if request['loan_status'] != 'ACTIVE':
        return reject('LOAN_NOT_ACTIVE')
    amount = request['requested_principal_cents']
    if type(amount) is not int or amount <= 0 or amount > request['outstanding_principal_cents']:
        return reject('INVALID_AMOUNT')

    def parse(text):
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', text):
            raise ValueError('date format')
        return date.fromisoformat(text)

    try:
        requested = parse(request['request_date'])
        settlement = parse(request['requested_settlement_date'])
    except ValueError:
        return reject('INVALID_DATE')
    if (settlement - requested).days < 14:
        return reject('NOTICE_TOO_SHORT')
    if settlement.weekday() >= 5:
        settlement += timedelta(days=7 - settlement.weekday())
    return {'status': 'SCHEDULED', 'reason': None, 'settlement_date': settlement.isoformat()}
"""


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int | None

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict):
            return None
        sent = data.get("prompt_tokens")
        received = data.get("completion_tokens")
        if type(sent) is not int or type(received) is not int or min(sent, received) < 0:
            return None
        details = data.get("prompt_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        if type(cached) is not int or not 0 <= cached <= sent:
            cached = None
        return cls(sent, received, cached)


@dataclass(frozen=True)
class Reply:
    message: dict
    usage: Usage | None
    served_model: str | None = None


class ModelError(Exception):
    def __init__(self, code, retryable=False, usage=None):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.usage = usage


class NoRedirect(HTTPRedirectHandler):
    """Keep credentials bound to the configured endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ChatEndpoint:
    def __init__(self, variant):
        self.variant = variant

    def complete(self, messages, tools, timeout):
        settings = self.variant
        headers = {"Content-Type": "application/json"}
        if settings.get("api_key_env"):
            headers["Authorization"] = "Bearer " + os.environ[settings["api_key_env"]]
        body = settings["parameters"] | {
            "model": settings["model"], "messages": messages, "stream": False, "n": 1,
        }
        if tools:
            body["tools"] = [TOOL]
        request = Request(settings["endpoint"], data=json.dumps(body).encode(), headers=headers)
        try:
            with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise ModelError(f"http_{code}", code in {408, 429, 500, 502, 503, 504}) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise ModelError("model_transport_error", True) from exc
        if len(raw) > MAX_RESPONSE:
            raise ModelError("model_response_too_large")
        try:
            item = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise ModelError("invalid_model_response") from exc
        if not isinstance(item, dict):
            raise ModelError("invalid_model_response")
        usage = Usage.parse(item.get("usage"))
        try:
            message = item["choices"][0]["message"]
        except (KeyError, TypeError, IndexError) as exc:
            raise ModelError("invalid_model_response", usage=usage) from exc
        if not isinstance(message, dict):
            raise ModelError("invalid_model_response", usage=usage)
        served_model = item.get("model")
        return Reply(message, usage, served_model if isinstance(served_model, str) else None)


class Fixture:
    def __init__(self, model):
        self.model = model

    def complete(self, messages, tools, timeout):
        time.sleep(min(0.012, timeout))
        if tools:
            seen = sum(message["role"] == "tool" for message in messages)
            fragments = ("policy", "openapi")
            if seen < len(fragments):
                name = fragments[seen]
                call = {
                    "id": "fixture-" + name,
                    "type": "function",
                    "function": {"name": "read_spec", "arguments": json.dumps({"name": name})},
                }
                return Reply({"role": "assistant", "content": None, "tool_calls": [call]},
                             Usage(320 + seen * 360, 24, seen * 160), self.model)
        source = REFERENCE
        if self.model == "fixture-short-notice":
            source = source.replace(".days < 14", ".days < 13")
        return Reply({"role": "assistant", "content": source},
                     Usage(1220, 315, 300 if tools else 0), self.model)
