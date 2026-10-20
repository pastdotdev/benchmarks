"""DolphinBench with past.dev as the agent's memory.

DolphinBench's runner sends every history message, then every test request, to `PastHarness`;
see README.md for how to run it. The harness keeps one past.dev project per persona:

- Ingestion. Each history message reaches the agent in a fresh conversation, as the benchmark
  requires, and the agent replies without tools. The message itself is what past.dev stores, as
  past.dev's connectors do: it is pushed with its date and its author, 100 to a push. The agent's
  reply is not stored, so nothing the agent did not actually do enters the memory. Replies are
  prepared a few messages ahead, each in its own conversation; the push order is the history's.
- Tests. A fresh conversation with the request's app tools. past.dev's recall for the request comes
  first, as a connector puts it before the prompt, and the agent can recall again with the
  `past_recall` tool. Recall is the only memory call the harness makes in this phase, and
  `verify_checkpoint` reads each project's request log and memory count to confirm nothing else
  reached it.

Environment: PAST_API_URL, PAST_MANAGEMENT_KEY (past_mk_...), OPENROUTER_API_KEY.
"""

from __future__ import annotations

import asyncio
import atexit
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import re
import secrets
import signal
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import yaml

from examples.mcp_connection import connect_apps
from harness.adapter import InteractionRecord
from harness.durable_json import save_json

from model import Model
from past_client import RECALL_LIMIT, RECALL_MAX_TOKENS, PastApi, PastApiError

HARNESS_VERSION = "1.2"
PERSONAS = {"morgan": "Morgan Chen", "alex": "Alex Valdez", "riley": "Riley Tanaka"}
RECALL_TOOL = "past_recall"
# past.dev's hosted index refuses a search over 1,000 characters (and answers 500), so a recall
# query keeps its first 1,000, cut at a word.
RECALL_QUERY_MAX = 1_000
SETTLEMENT_POLL_SECONDS = 5
# past.dev cloud's price of a data point: one credit per 350 bytes of content, at least one
# (CreditCost.ForBytes). Recall is included on Flex, the plan these runs are priced at. A deployment
# with no plan (Pilot, self-host) bills nothing, so its pushes are priced at these list rates.
BYTES_PER_CREDIT = 350

# What may reach a project after its checkpoint: reads, recall, and this harness's key management.
ALLOWED_AFTER_CHECKPOINT = (
    ("POST", re.compile(r"^/api/v1/recall$")),
    ("POST", re.compile(r"^/api/v1/projects/[^/]+/keys$")),
    ("DELETE", re.compile(r"^/api/v1/projects/[^/]+/keys/[^/]+$")),
)

DEFAULTS = {
    "model": "openai/gpt-5.6-luna",
    "reasoning_effort": "high",          # the paper's Hermes + Luna configuration
    "max_turns": 60,                     # the same
    "ingestion_reasoning_effort": "none",
    "ingest_batch": 100,
    "lookahead": 16,
    "credit_usd_per_1000": 0.30,         # past.dev's list price for bought credits
    "release_root": ".",
}

# The benchmark's assistant identity (DolphinBench harness/benchmark_soul.md, Apache-2.0), without
# Hermes' name. See NOTICE.md.
IDENTITY = (
    "You are a direct and helpful personal assistant for {name}. Treat the supplied conversation "
    "history and current request as coming from that user. Use the available tools when they are "
    "needed to complete the request. Do not claim access to services, files, accounts, contacts, "
    "or information that are not present in the conversation or tools."
)
INGESTION_PROMPT = IDENTITY + "\n\nReply to {name}'s message briefly."
TEST_PROMPT = IDENTITY + (
    "\n\nYour long-term memory of {name} is past.dev. The next message holds what it recalled for "
    "this request: dated excerpts of {name}'s earlier messages and what was learned from them. "
    "Before you act, call " + RECALL_TOOL + " for anything else the request depends on, such as "
    "people, decisions, preferences, dates, numbers or earlier commitments."
)
RECALL_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": RECALL_TOOL,
        "description": ("Search your long-term memory of the user (past.dev) for what they said in earlier "
                        "conversations. Returns dated excerpts with their sources."),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to look for, in plain words."}},
            "required": ["query"],
        },
    },
}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def utc_iso(moment: str) -> str:
    """A benchmark date (with offset, or a plain day) as UTC ISO-8601."""
    parsed = dt.datetime.fromisoformat(moment)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def end_of(day_or_moment: str) -> str:
    """A test's anchor: a plain day means the end of that day."""
    if len(day_or_moment) == 10:
        return f"{day_or_moment}T23:59:59Z"
    return utc_iso(day_or_moment)


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def append_line(path: Path, value: dict) -> None:
    """One durable JSON line: the record survives a crash right after the call it accounts for."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_lines(path: Path) -> list[dict]:
    # Split on newlines only: str.splitlines() also breaks on U+2028 inside a JSON string.
    return [json.loads(line) for line in path.read_text().split("\n") if line.strip()] if path.exists() else []


def list_credits(content: str) -> int:
    return max(1, -(-len(content.encode("utf-8")) // BYTES_PER_CREDIT))


class PastHarness:
    def __init__(self, options: dict, work_dir: Path) -> None:
        unknown = set(options) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"Unknown options: {sorted(unknown)}")
        self.options = {**DEFAULTS, **options}
        self.work_dir = Path(work_dir)
        self.state = self.work_dir / "past"
        self.release_root = Path(self.options["release_root"]).resolve()
        # Construction makes no model or memory call; connections open on first use.
        self._models: dict[str, Model] = {}
        self._management: PastApi | None = None
        self._projects: dict[str, tuple[PastApi, str]] = {}  # persona → (client, slug)
        self._sessions: dict[str, list[dict]] = {}
        self._replies: dict[tuple[str, str], Future] = {}
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._closed = False
        atexit.register(self._close)
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    # --- Identity -----------------------------------------------------------------------------

    def identity(self) -> dict:
        here = Path(__file__).resolve().parent
        code = {name: hashlib.sha256((here / name).read_bytes()).hexdigest()
                for name in ("past_harness.py", "past_client.py", "model.py")}
        versions = {"python": sys.version.split()[0]}
        for package in ("httpx", "mcp", "PyYAML"):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = None
        return {
            "harness": "past.dev", "harness_version": HARNESS_VERSION, "code": code, "dependencies": versions,
            "agent": {
                "model": self.options["model"], "reasoning_effort": self.options["reasoning_effort"],
                "max_turns": self.options["max_turns"],
                "test_prompt_sha256": hashlib.sha256(TEST_PROMPT.encode()).hexdigest(),
                "ingestion": {"reasoning_effort": self.options["ingestion_reasoning_effort"], "tools": [],
                              "prompt_sha256": hashlib.sha256(INGESTION_PROMPT.encode()).hexdigest()},
            },
            "memory": {
                "provider": "past.dev", "api_host": urlparse(os.environ.get("PAST_API_URL", "")).netloc,
                "ingestion": {"stored": "each history message with its date, local time and author",
                              "items_per_push": self.options["ingest_batch"], "agent_replies_stored": False},
                "recall": {"limit": RECALL_LIMIT, "maxTokens": RECALL_MAX_TOKENS,
                           "automatic_before_request": True, "tool": RECALL_TOOL},
                "internal_models": ("past.dev runs its own extraction, embedding and ranking models on the "
                                    "server. Those calls are not in the transcript; their cost is in the credits."),
                "credit_usd_per_1000": self.options["credit_usd_per_1000"],
                "credits": ("billed by the deployment; where it bills none (no plan), each pushed data point at "
                            f"past.dev cloud's rate, one credit per {BYTES_PER_CREDIT} bytes, at least one; recall "
                            "included, as on Flex"),
            },
        }

    # --- Interactions -------------------------------------------------------------------------

    def run_interaction(self, request) -> InteractionRecord:
        if request.phase == "ingestion":
            return self._ingest(request)
        return asyncio.run(self._test(request))

    def _ingest(self, request) -> InteractionRecord:
        persona = request.persona
        self._mark_phase(persona, "ingestion_start")
        reply = self._reply(persona, request).result()
        if reply["dated_message"] != request.dated_message:
            raise ValueError(f"{persona}/{request.interaction_id}: the prepared reply is for another message")
        self._enqueue(persona, request)
        return InteractionRecord(
            settings={"model": self.options["model"], "reasoning_effort": self.options["ingestion_reasoning_effort"]},
            messages=[
                {"role": "system", "content": INGESTION_PROMPT.format(name=PERSONAS[persona])},
                {"role": "user", "content": request.dated_message},
                {"role": "assistant", "content": reply["content"], "usage": reply["usage"]},
            ],
            duration_ms=reply["duration_ms"],
        )

    def _reply(self, persona: str, request) -> Future:
        """This message's reply, and the next few prepared in parallel, each in its own conversation."""
        sessions = self._history(persona)
        index = int(request.interaction_id) - 1
        if sessions[index]["messages"][0].strip() != request.message:
            raise ValueError(f"{persona}/{request.interaction_id}: the release and the runner disagree")
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self.options["lookahead"])
            model = self._model(self.options["ingestion_reasoning_effort"])
            for ahead in sessions[index:index + self.options["lookahead"]]:
                key = (persona, ahead["id"])
                if key not in self._replies:
                    dated = f"[{ahead['narrative_date']}] {ahead['messages'][0].strip()}"
                    self._replies[key] = self._pool.submit(self._prepare_reply, model, persona, ahead["id"], dated)
            return self._replies.pop((persona, request.interaction_id))

    def _prepare_reply(self, model: Model, persona: str, session_id: str, dated_message: str) -> dict:
        path = self.state / "replies" / persona / f"{session_id}.json"
        saved = read_json(path)
        if saved and saved["dated_message"] == dated_message:
            return saved
        message, usage, cost, duration_ms = model.complete([
            {"role": "system", "content": INGESTION_PROMPT.format(name=PERSONAS[persona])},
            {"role": "user", "content": dated_message},
        ])
        append_line(self.state / "costs" / "ingestion.jsonl", {"persona": persona, "session": session_id,
                                                                "cost_usd": cost})
        reply = {"dated_message": dated_message, "content": message.get("content") or "", "usage": usage,
                 "duration_ms": duration_ms}
        save_json(path, reply)
        return reply

    def _enqueue(self, persona: str, request) -> None:
        """The message joins its persona's queue; a full queue is pushed. The queue survives restarts."""
        queue_path = self.state / "queue" / f"{persona}.json"
        queue = read_json(queue_path, [])
        item_id = f"{persona}-{request.interaction_id}"
        if not any(item["id"] == item_id for item in queue) and item_id not in self._pushed_ids(persona):
            queue.append({
                "id": item_id,
                "content": request.message,
                "label": f"{PERSONAS[persona]}, message {request.interaction_id}",
                "timestamp": utc_iso(request.narrative_time),
                "identity": self._identity(persona),
                # The user's own clock: the UTC timestamp can fall on the next day.
                "metadata": {"role": "user", "author": PERSONAS[persona], "localTime": request.narrative_time},
            })
            save_json(queue_path, queue)
        if len(queue) >= self.options["ingest_batch"]:
            self._flush(persona)

    def _flush(self, persona: str) -> None:
        queue_path = self.state / "queue" / f"{persona}.json"
        queue = read_json(queue_path, [])
        if not queue:
            return
        ids = [item["id"] for item in queue]
        receipts_path = self.state / "receipts" / f"{persona}.json"
        receipts = read_json(receipts_path, [])
        if not any(receipt["items"] == ids for receipt in receipts):
            client, _ = self._project(persona)
            receipt = client.ingest_batch(queue, "dolphinbench-" + digest(ids)[:40])
            receipts.append({"ingestionId": receipt["ingestionId"], "items": ids,
                             "contentSha256": digest([item["content"] for item in queue]),
                             "listCredits": sum(list_credits(item["content"]) for item in queue)})
            save_json(receipts_path, receipts)
        save_json(queue_path, [])

    def _pushed_ids(self, persona: str) -> set[str]:
        receipts = read_json(self.state / "receipts" / f"{persona}.json", [])
        return {item for receipt in receipts for item in receipt["items"]}

    async def _test(self, request) -> InteractionRecord:
        persona = request.persona
        self._mark_phase(persona, "tests_start")
        client, _ = self._project(persona)
        name = PERSONAS[persona]
        model = self._model(self.options["reasoning_effort"])
        as_of = end_of(request.narrative_time)
        ledger = self.state / "costs" / "tests" / persona / f"{request.interaction_id}.jsonl"
        attempt = uuid.uuid4().hex

        async with connect_apps(request.apps) as apps:
            app_tools = (await apps.list_tools()).tools
            if any(tool.name == RECALL_TOOL for tool in app_tools):
                raise ValueError(f"An app tool is named {RECALL_TOOL}")
            tools = [{"type": "function", "function": {
                "name": tool.name, "description": tool.description or "",
                "parameters": tool.inputSchema or {"type": "object", "properties": {}}}} for tool in app_tools]
            tools.append(RECALL_TOOL_DEFINITION)

            recalled = await asyncio.to_thread(self._recall, client, request.message, as_of, persona)
            opening = [
                {"role": "system", "content": TEST_PROMPT.format(name=name)},
                {"role": "system", "content": "past.dev recalled for this request:\n"
                                              + json.dumps(recalled, ensure_ascii=False, separators=(",", ":"))},
                {"role": "user", "content": request.dated_message},
            ]
            conversation = [dict(message) for message in opening]
            recorded = [dict(message) for message in opening]

            for turn in range(self.options["max_turns"] + 1):
                final_turn = turn == self.options["max_turns"]
                message, usage, cost, duration_ms = await asyncio.to_thread(
                    model.complete, conversation, tools, "none" if final_turn else None)
                append_line(ledger, {"attempt": attempt, "turn": turn, "cost_usd": cost})
                calls = [] if final_turn else [parse_call(call, f"call-{turn}-{index}")
                                                for index, call in enumerate(message.get("tool_calls") or [])]
                entry = {"role": "assistant", "content": message.get("content") or "", "usage": usage,
                         "duration_ms": duration_ms}
                if calls:
                    entry["tool_calls"] = [{"id": c["id"], "name": c["name"], "arguments": c["arguments"]}
                                           for c in calls]
                recorded.append(entry)
                conversation.append({key: message[key] for key in ("content", "tool_calls", "reasoning_details")
                                     if message.get(key) is not None} | {"role": "assistant"})
                if not calls:
                    break
                for call in calls:
                    if call["error"]:
                        content = json.dumps({"error": call["error"]})
                    elif call["name"] == RECALL_TOOL:
                        query = str(call["arguments"].get("query") or request.message)
                        page = await asyncio.to_thread(self._recall, client, query, as_of, persona)
                        content = json.dumps(page, ensure_ascii=False, separators=(",", ":"))
                    else:
                        result = await apps.call_tool(call["name"], call["arguments"])
                        content = "\n".join(getattr(part, "text", "") or json.dumps(part.model_dump())
                                            for part in result.content) or "{}"
                    recorded.append({"role": "tool", "tool_call_id": call["id"], "content": content})
                    conversation.append({"role": "tool", "tool_call_id": call["id"], "content": content})

        return InteractionRecord(
            settings={"model": self.options["model"], "reasoning_effort": self.options["reasoning_effort"],
                      "tools": tools, "parallel_tool_calls": True},
            messages=recorded,
        )

    def _recall(self, client: PastApi, query: str, as_of: str, persona: str) -> dict:
        """One recall. A refusal past the client's retries reaches the agent as an error, not the run."""
        if len(query) > RECALL_QUERY_MAX:
            query = query[:RECALL_QUERY_MAX].rsplit(" ", 1)[0]
        try:
            return client.recall(query, as_of, self._identity(persona))
        except PastApiError as error:
            return {"error": f"past.dev recall failed: {str(error)[:300]}", "results": []}

    # --- Memory lifecycle ---------------------------------------------------------------------

    def freeze(self, persona: str) -> dict:
        """Push what is queued, wait until past.dev has processed every push, and record the result."""
        self._flush(persona)
        client, slug = self._project(persona)
        receipts = read_json(self.state / "receipts" / f"{persona}.json", [])
        if not receipts:
            raise ValueError(f"{persona}: nothing was ingested")
        wait_until_processed(client, receipts[-1]["ingestionId"])
        # A push past.dev completed without keeping one memory is not a memory: stop before any test
        # instead of measuring a store whose understanding silently failed.
        empty = [index + 1 for index, receipt in enumerate(receipts)
                 if not any(int(item.get("memories") or 0) for item in
                            client.explain(receipt["ingestionId"]).get("items") or [])]
        if empty:
            raise RuntimeError(f"{persona}: past.dev kept no memory from push(es) {empty}; see their /explain")
        self._mark_phase(persona, "ingestion_end", replace=True)
        checkpoint = {
            "provider": "past.dev", "project": slug, "pushes": len(receipts),
            "items": sum(len(receipt["items"]) for receipt in receipts),
            "receiptsSha256": digest(receipts), "lastIngestionId": receipts[-1]["ingestionId"],
            "memoryCount": self._memory_count(slug), "frozenAt": now_iso(),
        }
        save_json(self.state / "checkpoints" / f"{persona}.json", checkpoint)
        return checkpoint

    def verify_checkpoint(self, persona: str, checkpoint: dict) -> None:
        """The project still holds exactly what was frozen, and nothing but reads reached it since."""
        receipts = read_json(self.state / "receipts" / f"{persona}.json", [])
        if checkpoint.get("receiptsSha256") != digest(receipts):
            raise ValueError(f"{persona}: the saved pushes no longer match the checkpoint")
        if read_json(self.state / "queue" / f"{persona}.json", []):
            raise ValueError(f"{persona}: messages were queued after the checkpoint")
        client, slug = self._project(persona)
        if slug != checkpoint["project"]:
            raise ValueError(f"{persona}: the checkpoint names project {checkpoint['project']}, not {slug}")
        status = client.ingestion_status(checkpoint["lastIngestionId"])
        if str(status.get("status", "")).lower() != "completed" or not status.get("settled"):
            raise ValueError(f"{persona}: the project is no longer fully processed")
        count = self._memory_count(slug)
        if count != checkpoint["memoryCount"]:
            raise ValueError(f"{persona}: the project holds {count} memories, {checkpoint['memoryCount']} at the checkpoint")
        writes = [row for row in client.requests(checkpoint["frozenAt"], now_iso())
                  if str(row.get("method", "")).upper() != "GET"
                  and not any(row.get("method", "").upper() == method and pattern.match(str(row.get("path", "")))
                              for method, pattern in ALLOWED_AFTER_CHECKPOINT)]
        if writes:
            first = writes[0]
            raise ValueError(f"{persona}: {len(writes)} write(s) reached the project after the checkpoint, "
                             f"the first {first.get('method')} {first.get('path')} at {first.get('receivedAt')}")

    def _memory_count(self, slug: str) -> int:
        project = next((p for p in self._management_client().list_projects() if p["slug"] == slug), None)
        if project is None:
            raise ValueError(f"Project {slug} no longer exists")
        return int(project["memoryCount"])

    # --- Cost ---------------------------------------------------------------------------------

    def total_cost_usd(self, phase: str) -> float:
        """Model spend from each response's reported cost, plus the credits past.dev billed, at list price."""
        if phase == "ingestion":
            model = sum(line["cost_usd"] for line in read_lines(self.state / "costs" / "ingestion.jsonl"))
        elif phase == "tests":
            model = sum(line["cost_usd"] for path in (self.state / "costs" / "tests").glob("*/*.jsonl")
                        for line in read_lines(path))
            for persona in PERSONAS:
                self._mark_phase(persona, "tests_end", replace=True)
        else:
            raise ValueError(f"Unknown phase {phase}")
        credits = 0.0
        for persona in PERSONAS:
            marks = read_json(self.state / "phases" / f"{persona}.json", {})
            start, end = marks.get(f"{phase}_start"), marks.get(f"{phase}_end")
            if not start or not end:
                raise ValueError(f"{persona}: the {phase} phase has no recorded start and end")
            client, _ = self._project(persona)
            # Every request past.dev billed in the phase, whatever its billing period.
            rows = client.requests(start, end)
            billed = sum(float(row.get("credits") or 0.0) for row in rows)
            if billed == 0 and rows and phase == "ingestion":
                # A deployment with no plan bills nothing: the pushes at past.dev cloud's list rate.
                billed = sum(receipt.get("listCredits", 0)
                             for receipt in read_json(self.state / "receipts" / f"{persona}.json", []))
            credits += billed
        return round(model + credits * self.options["credit_usd_per_1000"] / 1000, 6)

    def _mark_phase(self, persona: str, moment: str, replace: bool = False) -> None:
        path = self.state / "phases" / f"{persona}.json"
        marks = read_json(path, {})
        if moment in marks and not replace:
            return
        marks[moment] = now_iso()
        save_json(path, marks)

    # --- Connections --------------------------------------------------------------------------

    def _model(self, effort: str) -> Model:
        if effort not in self._models:
            self._models[effort] = Model(self.options["model"], effort)
        return self._models[effort]

    def _history(self, persona: str) -> list[dict]:
        if persona not in self._sessions:
            path = self.release_root / "registry" / "personas" / persona / "life_sim.yaml"
            self._sessions[persona] = yaml.safe_load(path.read_text())["sessions"]
        return self._sessions[persona]

    def _identity(self, persona: str) -> str:
        return PERSONAS[persona].lower().replace(" ", "-")

    def _management_client(self) -> PastApi:
        if self._management is None:
            api_url = os.environ.get("PAST_API_URL", "").strip()
            management_key = os.environ.get("PAST_MANAGEMENT_KEY", "").strip()
            if not api_url or not management_key:
                raise ValueError("Set PAST_API_URL and PAST_MANAGEMENT_KEY before running")
            self._management = PastApi(api_url, management_key)
            self._revoke_recorded_keys()
        return self._management

    def _project(self, persona: str) -> tuple[PastApi, str]:
        """The persona's project, created once per run, with a key made for this process."""
        with self._lock:
            if persona in self._projects:
                return self._projects[persona]
            management = self._management_client()
            projects_path = self.state / "projects.json"
            projects = read_json(projects_path, {})
            # A tag per run keeps every run's projects apart, so each memory starts empty.
            projects.setdefault("tag", secrets.token_hex(4))
            if persona not in projects:
                projects[persona] = {"name": f"dolphinbench-{persona}-{projects['tag']}"}
                save_json(projects_path, projects)
            entry = projects[persona]
            if "slug" not in entry:
                entry["slug"] = management.create_project(entry["name"])
                save_json(projects_path, projects)
            slug = entry["slug"]
            key = management.create_project_key(slug, "dolphinbench")
            self._record_key(slug, key["id"])
            client = PastApi(os.environ["PAST_API_URL"], key["key"])
            if not entry.get("identityRegistered"):
                client.register_identities([{"identity": self._identity(persona),
                                             "traits": {"name": PERSONAS[persona]}}])
                entry["identityRegistered"] = True
                save_json(projects_path, projects)
            self._projects[persona] = (client, slug)
            return self._projects[persona]

    # --- Keys: each process's keys are recorded by id (never their value) and revoked ---------

    def _record_key(self, slug: str, key_id: str) -> None:
        path = self.state / "keys.json"
        keys = read_json(path, [])
        keys.append({"project": slug, "id": key_id})
        save_json(path, keys)

    def _revoke_recorded_keys(self) -> None:
        path = self.state / "keys.json"
        remaining = []
        for key in read_json(path, []):
            try:
                self._management.revoke_project_key(key["project"], key["id"])
            except Exception as error:  # an already-revoked key answers 404
                if getattr(error, "status", None) != 404:
                    print(f"past.dev: could not revoke key {key['id']} of {key['project']}: {error}", file=sys.stderr)
                    remaining.append(key)
        if path.exists() or remaining:
            save_json(path, remaining)

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
        for client, _ in self._projects.values():
            client.close()
        if self._management is not None:
            self._revoke_recorded_keys()
            self._management.close()


def parse_call(call: dict, fallback_id: str) -> dict:
    """A tool call as the model returned it; arguments that are not a JSON object become an error."""
    function = call.get("function") or {}
    raw = function.get("arguments") or "{}"
    try:
        arguments = json.loads(raw) if isinstance(raw, str) else raw
        error = None if isinstance(arguments, dict) else "arguments must be a JSON object"
    except json.JSONDecodeError:
        arguments, error = None, "arguments are not valid JSON"
    return {"id": call.get("id") or fallback_id, "name": function.get("name") or "",
            "arguments": arguments if isinstance(arguments, dict) else {"raw": str(raw)}, "error": error}


def wait_until_processed(client: PastApi, last_ingestion_id: str) -> None:
    """The last push completed and the project settled: every memory is indexed and recallable."""
    while True:
        status = client.ingestion_status(last_ingestion_id)
        state = str(status.get("status", "")).lower()
        if state == "failed":
            raise RuntimeError(f"Push {last_ingestion_id} failed: {status.get('failure')}")
        if status.get("blocked"):
            raise RuntimeError("A push in this project failed, so the project cannot finish processing")
        if state == "completed" and status.get("settled"):
            return
        time.sleep(SETTLEMENT_POLL_SECONDS)
