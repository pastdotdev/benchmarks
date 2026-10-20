"""Runs the Hermes agent's Azure Foundry calls through OpenRouter, on Azure.

DolphinBench's Hermes + Luna configuration calls an Azure Foundry endpoint for `gpt-5.6-luna`
through the OpenAI Responses API, streamed. This relay listens for those calls and forwards them to
OpenRouter's `openai/gpt-5.6-luna`, pinned to OpenRouter's Azure provider with no fallback: the
same model, on the same host, with the request's own reasoning effort, tools and input. Responses
stream back as OpenRouter sends them. Hermes and its profile are unchanged; only the endpoint's
address is the relay's.

    export OPENROUTER_API_KEY=...
    export AZURE_FOUNDRY_API_KEY=$(python -c "import secrets; print(secrets.token_hex(16))")
    export AZURE_OPENAI_BASE_URL=http://host.docker.internal:8766/openai/v1
    python agent_relay.py &

An answer OpenRouter completes with no output at all (the model had nothing to say, typically
after a tool call) is relayed as is; Hermes takes it for a broken response and sends the same
request again, eight times over about nine minutes. The relay answers a repeat of a request whose
answer was empty with a 400 `empty_completion` instead of asking again, so the turn ends there,
as the model left it.

AZURE_FOUNDRY_API_KEY is only a local password between Hermes and the relay. A key of the form
`<password>.<tag>` tags the call: each call's start, then its end with the cost OpenRouter reports,
is appended to AGENT_RELAY_LOG (default agent-relay.jsonl) and, for a tagged call, to
AGENT_RELAY_TAGS/<tag>.jsonl, so a caller can sum what one agent turn cost.
"""

import hashlib
import http.server
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx

PORT = int(os.environ.get("AGENT_RELAY_PORT", "8766"))
OPENROUTER = "https://openrouter.ai/api/v1"
ROUTE = re.compile(r"^(?:/[^?]*)?/(responses|chat/completions)(\?.*)?$")
TAG = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,199}$")
RETRY_DELAYS_SECONDS = (2, 5, 15, 30, 60)
LOG_LOCK = threading.Lock()
EMPTY_LOCK = threading.Lock()
EMPTY_ANSWERS: set[str] = set()  # requests whose answer completed with no output


class Relay(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self._tag() is False:
            return self._answer(401, {"error": "unauthorized"})
        if self.path.split("?")[0].endswith("/models"):
            return self._answer(200, {"object": "list", "data": [{"id": "gpt-5.6-luna", "object": "model"}]})
        self._answer(404, {"error": "not found"})

    def do_POST(self):
        match = ROUTE.match(self.path)
        tag = self._tag()
        if tag is False:
            return self._answer(401, {"error": "unauthorized"})
        if not match:
            return self._answer(404, {"error": "not found"})
        endpoint = match.group(1)
        call = uuid.uuid4().hex
        log(tag, {"event": "start", "call": call, "endpoint": endpoint})
        try:
            self._forward(endpoint, call, tag)
        except BaseException as error:
            log(tag, {"event": "end", "call": call, "status": None, "cost_usd": None, "error": str(error)[:300]})
            raise

    def _forward(self, endpoint, call, tag):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        request_key = hashlib.sha256(raw).hexdigest()
        body = json.loads(raw)
        with EMPTY_LOCK:
            repeated = endpoint == "responses" and request_key in EMPTY_ANSWERS
        if repeated:
            log(tag, {"event": "end", "call": call, "status": 400, "cost_usd": 0.0, "empty_completion": "repeat"})
            return self._answer(400, {"error": {
                "message": "empty_completion: the model already answered this exact request with no output",
                "type": "invalid_request_error", "code": "empty_completion"}})
        model = str(body.get("model") or "")
        body["model"] = model if "/" in model else f"openai/{model}"
        body["provider"] = {"order": ["azure"], "allow_fallbacks": False}
        if endpoint == "chat/completions":
            body["usage"] = {"include": True}
        headers = {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}", "Content-Type": "application/json"}
        # Retried only until an answer starts: once it streams on, it is never sent twice.
        for delay in (*RETRY_DELAYS_SECONDS, None):
            try:
                with httpx.stream("POST", f"{OPENROUTER}/{endpoint}", json=body, headers=headers,
                                  timeout=httpx.Timeout(900, connect=30)) as response:
                    if (response.status_code == 429 or response.status_code >= 500) and delay is not None:
                        response.read()
                        time.sleep(delay)
                        continue
                    return self._relay(response, body, call, tag, request_key)
            except httpx.HTTPError as error:
                if delay is None:
                    log(tag, {"event": "end", "call": call, "status": 502, "cost_usd": 0.0, "error": str(error)[:300]})
                    return self._answer(502, {"error": str(error)})
                time.sleep(delay)
        raise AssertionError("unreachable")

    def _relay(self, response, body, call, tag, request_key):
        """Sends OpenRouter's answer on as it arrives, then logs what OpenRouter billed for it."""
        self.send_response(response.status_code)
        self.send_header("Content-Type", response.headers.get("content-type", "application/json"))
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        head, tail, error = b"", b"", None
        try:
            for chunk in response.iter_raw():
                if not chunk:
                    continue
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
                if len(head) < 16_000:
                    head += chunk[:16_000 - len(head)]
                tail = (tail + chunk)[-200_000:]
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (httpx.HTTPError, OSError) as exc:  # a broken stream ends the call; it is not resent
            error = str(exc)[:300]
        usage = usage_from(tail) if response.status_code == 200 else {}
        cost = usage.get("cost") if response.status_code == 200 else 0.0
        generation = generation_id(head)
        if cost is None and generation:
            cost = generation_cost(generation)
        empty = response.status_code == 200 and not error and completed_output(tail) == []
        if empty:
            with EMPTY_LOCK:
                EMPTY_ANSWERS.add(request_key)
        log(tag, {"event": "end", "call": call, "status": response.status_code, "model": body.get("model"),
                  "generation": generation, "cost_usd": None if cost is None else float(cost),
                  "usage": usage, **({"error": error} if error else {}), **({"empty_completion": "first"} if empty else {})})

    def _tag(self):
        """None for the bare password, the tag for `<password>.<tag>`, False for anything else."""
        token = os.environ["AZURE_FOUNDRY_API_KEY"]
        bearer = self.headers.get("Authorization", "")
        offered = self.headers.get("api-key") or (bearer[7:] if bearer.startswith("Bearer ") else "")
        if offered == token:
            return None
        if offered.startswith(token + ".") and TAG.match(offered[len(token) + 1:]):
            return offered[len(token) + 1:]
        return False

    def _answer(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def usage_from(tail: bytes) -> dict:
    """The usage OpenRouter reports: the final streamed event's, or the whole answer's."""
    text = tail.decode("utf-8", "replace")
    candidates = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")] or [text]
    for candidate in reversed(candidates):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        usage = (value.get("response") or {}).get("usage") or value.get("usage")
        if usage:
            return usage
    return {}


def completed_output(tail: bytes) -> list | None:
    """The output list of a completed Responses answer, streamed or not; None if unknown."""
    text = tail.decode("utf-8", "replace")
    for candidate in reversed([line[5:].strip() for line in text.splitlines() if line.startswith("data:")] or [text]):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        response = value.get("response") if isinstance(value.get("response"), dict) else value
        if response.get("status") == "completed" and isinstance(response.get("output"), list):
            return response["output"]
    return None


def generation_id(head: bytes) -> str | None:
    """The OpenRouter generation id the answer starts with, streamed or not."""
    text = head.decode("utf-8", "replace")
    for candidate in [line[5:].strip() for line in text.splitlines() if line.startswith("data:")] or [text]:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        found = (value.get("response") or {}).get("id") or value.get("id")
        if found:
            return str(found)
    return None


def generation_cost(generation: str) -> float | None:
    """What OpenRouter billed for a generation whose answer did not say; its stats can lag a little."""
    for delay in (2, 3, 5, 10, 20):
        time.sleep(delay)
        try:
            response = httpx.get(f"{OPENROUTER}/generation", params={"id": generation}, timeout=30,
                                 headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"})
            if response.status_code == 200:
                cost = (response.json().get("data") or {}).get("total_cost")
                if cost is not None:
                    return float(cost)
        except (httpx.HTTPError, ValueError):
            pass
    return None


def log(tag, record):
    line = json.dumps({"at": time.time(), "tag": tag, **record}) + "\n"
    with LOG_LOCK:
        with open(os.environ.get("AGENT_RELAY_LOG", "agent-relay.jsonl"), "a") as handle:
            handle.write(line)
        if tag and os.environ.get("AGENT_RELAY_TAGS"):
            directory = Path(os.environ["AGENT_RELAY_TAGS"])
            directory.mkdir(parents=True, exist_ok=True)
            with open(directory / f"{tag}.jsonl", "a") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())


if __name__ == "__main__":
    for name in ("OPENROUTER_API_KEY", "AZURE_FOUNDRY_API_KEY"):
        if not os.environ.get(name):
            sys.exit(f"Set {name}")
    http.server.ThreadingHTTPServer((os.environ.get("AGENT_RELAY_HOST", "127.0.0.1"), PORT), Relay).serve_forever()
