"""Runs DolphinBench's fixed grader through OpenRouter, on Azure, for those without an Azure account.

DolphinBench grades with Azure `gpt-5.6-sol`, medium reasoning, and its runner calls an Azure
deployment URL. This relay listens on 127.0.0.1 for exactly that call and forwards it to
OpenRouter's `openai/gpt-5.6-sol`, pinned to OpenRouter's Azure provider with no fallback: the same
model, on the same host, with the request's own reasoning effort, response format and token
budget. The runner and the graders are unchanged.

    export OPENROUTER_API_KEY=...
    export AZURE_OPENAI_API_KEY=$(python -c "import secrets; print(secrets.token_hex(16))")
    export AZURE_OPENAI_ENDPOINT=http://127.0.0.1:8765
    python judge_relay.py &          # then run the DolphinBench stages as usual

AZURE_OPENAI_API_KEY is only a local password between the runner and the relay. Each graded call's
cost is appended to JUDGE_RELAY_LOG (default judge-relay.jsonl). A submission graded this way should
say so: its judge settings name Azure gpt-5.6-sol, which is what ran, but not Azure's own endpoint.
"""

import http.server
import json
import os
import re
import sys
import time

import httpx

PORT = int(os.environ.get("JUDGE_RELAY_PORT", "8765"))
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
ROUTE = re.compile(r"^/openai/deployments/(gpt-5\.6-sol)/chat/completions(\?.*)?$")
RETRY_DELAYS_SECONDS = (2, 5, 15, 30, 60)


class Relay(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        match = ROUTE.match(self.path)
        if not match or self.headers.get("api-key") != os.environ["AZURE_OPENAI_API_KEY"]:
            return self._answer(404, {"error": "not found"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        payload = {
            "model": f"openai/{match.group(1)}",
            "messages": body["messages"],
            "reasoning": {"effort": body.get("reasoning_effort", "medium")},
            "provider": {"order": ["azure"], "allow_fallbacks": False},
            "usage": {"include": True},
        }
        if body.get("response_format"):
            payload["response_format"] = body["response_format"]
        if body.get("max_completion_tokens"):
            payload["max_tokens"] = body["max_completion_tokens"]
        status, answer = forward(payload)
        if status == 200:
            usage = answer.get("usage") or {}
            with open(os.environ.get("JUDGE_RELAY_LOG", "judge-relay.jsonl"), "a") as log:
                log.write(json.dumps({"at": time.time(), "provider": answer.get("provider"),
                                      "cost_usd": usage.get("cost"), "prompt_tokens": usage.get("prompt_tokens"),
                                      "completion_tokens": usage.get("completion_tokens")}) + "\n")
        self._answer(status, answer)

    def _answer(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def forward(payload: dict) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"}
    for delay in (*RETRY_DELAYS_SECONDS, None):
        try:
            response = httpx.post(OPENROUTER, json=payload, headers=headers, timeout=600)
        except httpx.HTTPError as error:
            if delay is None:
                return 502, {"error": str(error)}
            time.sleep(delay)
            continue
        retry = response.status_code == 429 or response.status_code >= 500 or (
            response.status_code == 200 and response.json().get("error"))
        if retry and delay is not None:
            time.sleep(delay)
            continue
        return response.status_code, response.json()
    raise AssertionError("unreachable")


if __name__ == "__main__":
    for name in ("OPENROUTER_API_KEY", "AZURE_OPENAI_API_KEY"):
        if not os.environ.get(name):
            sys.exit(f"Set {name}")
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Relay).serve_forever()
