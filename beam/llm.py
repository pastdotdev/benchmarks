"""The answer and judge model, called through OpenRouter.

One model answers and judges: openai/gpt-5.6-luna, with reasoning off.

A request is retried on a rate limit, a server error, a dropped connection or an unreadable HTTP
response: four attempts in all. A request that still fails, an empty reply, or a judge reply that is
not JSON raises; the caller scores that answer or verdict 0, as ExaBase's adapter does. A refusal of
the account itself (401, 402, 403: a wrong key, no credits) stops the run instead, since it would
otherwise score every question 0.
"""

import asyncio
import json
import os

import httpx

from log import log

MODEL = "openai/gpt-5.6-luna"
REASONING_EFFORT = "none"
BASE_URL = "https://openrouter.ai/api/v1"
TIMEOUT_SECONDS = 90
RETRY_DELAYS_SECONDS = (1, 2, 4)

_http: httpx.AsyncClient | None = None


class ModelError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


def stops_the_run(error: BaseException) -> bool:
    """An account refusal from OpenRouter or Past: the setup is wrong, not this one question."""
    return getattr(error, "status", None) in (401, 402, 403)


async def complete_text(prompt: str) -> str:
    """The model's complete free-form reply to one user message."""
    response = await _post({
        "model": MODEL,
        "reasoning": {"effort": REASONING_EFFORT},
        "usage": {"include": True},
        "messages": [{"role": "user", "content": prompt}],
    })
    text = _reply_text(response)
    if not text.strip():
        raise ValueError("The model returned an empty answer")
    return text


async def complete_json(prompt: str, schema: dict) -> dict:
    """A reply held to a strict JSON schema, from a provider that enforces it."""
    response = await _post({
        "model": MODEL,
        "reasoning": {"effort": REASONING_EFFORT},
        "usage": {"include": True},
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "benchmark_response",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": schema["properties"],
                    "required": schema["required"],
                    "additionalProperties": False,
                },
            },
        },
        "provider": {"require_parameters": True},
    })
    text = _reply_text(response)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # A reply wrapped in extra text still counts if it holds one JSON object.
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


async def close() -> None:
    if _http is not None:
        await _http.aclose()


# --- Transport -------------------------------------------------------------------------------

def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is required")
        _http = httpx.AsyncClient(base_url=BASE_URL, headers={"Authorization": f"Bearer {key}"},
                                  timeout=TIMEOUT_SECONDS)
    return _http


async def _post(payload: dict) -> dict:
    attempts = len(RETRY_DELAYS_SECONDS) + 1
    for attempt in range(attempts):
        try:
            return await _post_once(payload)
        except (httpx.HTTPError, ModelError, ValueError) as error:
            retryable = error.retryable if isinstance(error, ModelError) else True
            if not retryable or attempt == attempts - 1:
                raise
            log(f"model call failed ({str(error)[:120]}); retrying in {RETRY_DELAYS_SECONDS[attempt]}s")
            await asyncio.sleep(RETRY_DELAYS_SECONDS[attempt])
    raise AssertionError("unreachable")


async def _post_once(payload: dict) -> dict:
    response = await _client().post("/chat/completions", json=payload)
    if response.is_error:
        raise ModelError(f"OpenRouter returned {response.status_code}: {response.text[:1_000]}",
                         retryable=response.status_code == 429 or response.status_code >= 500,
                         status=response.status_code)
    data = response.json()
    if not isinstance(data, dict):
        raise ModelError("OpenRouter returned a non-object response", retryable=True)
    if data.get("error"):
        raise ModelError(f"OpenRouter returned an error: {data['error']}", retryable=False)
    if not isinstance(data.get("choices"), list) or not data["choices"]:
        raise ModelError("OpenRouter returned no choices", retryable=False)
    return data


def _reply_text(response: dict) -> str:
    content = response["choices"][0]["message"]["content"]
    if content is None:
        return ""
    if isinstance(content, list):
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return str(content)
