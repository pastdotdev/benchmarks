"""The agent's model calls, through OpenRouter's chat completions API.

Each call returns the assistant message, its usage exactly as the API reported it, and its cost
in dollars. OpenRouter reports the cost with every response; with a bring-your-own-key account
the provider's charge is in `cost_details.upstream_inference_cost`, and both are counted.
"""

import os
import time

import httpx

BASE_URL = "https://openrouter.ai/api/v1"
TIMEOUT_SECONDS = 600
RETRY_DELAYS_SECONDS = (2, 5, 15, 30, 60)


class ModelError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Model:
    def __init__(self, model: str, reasoning_effort: str) -> None:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise ModelError("Set OPENROUTER_API_KEY before running the agent")
        self.model = model
        self.reasoning_effort = reasoning_effort
        self._http = httpx.Client(base_url=BASE_URL, timeout=TIMEOUT_SECONDS,
                                  headers={"Authorization": f"Bearer {key}"})

    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 tool_choice: str | None = None) -> tuple[dict, dict, float, float]:
        """One response: (message, usage, cost in dollars, duration in milliseconds)."""
        payload = {
            "model": self.model,
            "messages": messages,
            "reasoning": {"effort": self.reasoning_effort},
            "usage": {"include": True},
        }
        if tools:
            payload["tools"] = tools
            payload["parallel_tool_calls"] = True
            if tool_choice:
                payload["tool_choice"] = tool_choice
        body, duration_ms = self._post(payload)
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message")
        usage = body.get("usage")
        if not isinstance(message, dict) or not isinstance(usage, dict):
            raise ModelError(f"OpenRouter answered without a message or usage: {str(body)[:500]}")
        return message, reported_usage(usage), cost_usd(usage), duration_ms

    def _post(self, payload: dict) -> tuple[dict, float]:
        for attempt, delay in enumerate((*RETRY_DELAYS_SECONDS, None)):
            started = time.monotonic()
            try:
                response = self._http.post("/chat/completions", json=payload)
            except httpx.HTTPError as error:
                if delay is None:
                    raise ModelError(f"OpenRouter unreachable: {error}") from error
                time.sleep(delay)
                continue
            duration_ms = (time.monotonic() - started) * 1000
            if response.status_code == 429 or response.status_code >= 500:
                if delay is None:
                    raise ModelError(f"OpenRouter returned {response.status_code}: {response.text[:500]}",
                                     status=response.status_code)
                time.sleep(delay)
                continue
            if response.is_error:
                raise ModelError(f"OpenRouter returned {response.status_code}: {response.text[:500]}",
                                 status=response.status_code)
            body = response.json()
            if body.get("error"):
                # A provider error inside a 200: retried like a server error.
                if delay is None:
                    raise ModelError(f"OpenRouter provider error: {str(body['error'])[:500]}")
                time.sleep(delay)
                continue
            return body, duration_ms
        raise AssertionError("unreachable")


def reported_usage(usage: dict) -> dict:
    """The usage fields DolphinBench reads, copied from the response, never estimated."""
    result = {
        "prompt_tokens": int(usage["prompt_tokens"]),
        "completion_tokens": int(usage["completion_tokens"]),
        "total_tokens": int(usage["total_tokens"]),
    }
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached:
        result["prompt_tokens_details"] = {"cached_tokens": int(cached)}
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if reasoning:
        result["completion_tokens_details"] = {"reasoning_tokens": int(reasoning)}
    return result


def cost_usd(usage: dict) -> float:
    """OpenRouter's charge, plus the provider's own charge when the request ran on our key (BYOK)."""
    cost = float(usage.get("cost") or 0.0)
    if usage.get("is_byok"):
        cost += float((usage.get("cost_details") or {}).get("upstream_inference_cost") or 0.0)
    return cost
