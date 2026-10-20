"""past.dev memory plugin using the MemoryProvider interface.

Configuration lives in ``$HERMES_HOME/past.json``::

    {"base_url": "https://api.past.dev", "api_key": "past_...", "identity": "morgan-chen"}

- ``sync_turn`` keeps every completed turn: the user's message and the assistant's reply are
  appended to ``$HERMES_HOME/past/spool.jsonl``. The host pushes the spool to past.dev a hundred
  data points at a time, the way past.dev's connectors send a session rather than a request per
  message. Nothing is kept while the benchmark marks memory read-only.
- ``past_recall`` is how the agent reads its memory, one recall per query, as of the turn's own
  date when the message carries one (``[<ISO-8601>] message``). There is no automatic recall:
  Hermes adds a provider's prefetch to the user's message itself, and DolphinBench's submission
  format requires that message unchanged, so the system prompt asks the agent to recall first.

Standard library only, so the plugin needs nothing beyond Hermes itself.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.benchmark_memory import memory_is_read_only
from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# One recall page: at most 100 results within 8,000 tokens of evidence.
RECALL_LIMIT = 100
RECALL_MAX_TOKENS = 8_000
# past.dev's hosted index refuses a search over 1,000 characters.
RECALL_QUERY_MAX = 1_000
RECALL_TIMEOUT_SECONDS = 30
RETRY_DELAYS_SECONDS = (1, 2, 4)
DATED_MESSAGE = re.compile(r"^\[([^\]\n]{8,40})\]\s*(.*)$", re.DOTALL)

# One query per call: a recall page is 40-75K characters, and Hermes replaces a tool result over
# 100K characters with a 1,500-character preview, and a turn's results over 200K the same way.
RECALL_SCHEMA = {
    "name": "past_recall",
    "description": (
        "Search past.dev, your long-term memory of the user, for what was said in earlier "
        "conversations. Write one short, specific query naming the person, project, decision, "
        "date, number or wording that identifies the fact you need; call past_recall again for "
        "each further fact, at most three calls at once. Returns dated excerpts of the earlier "
        "conversations and what was learned from them, with their sources."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "One short, specific query for one fact."},
        },
        "required": ["query"],
    },
}


def split_dated(message: str) -> tuple[Optional[str], str]:
    """``[<moment>] text`` → (moment, text); a message without a date is returned whole."""
    match = DATED_MESSAGE.match((message or "").strip())
    if not match:
        return None, (message or "").strip()
    moment = match.group(1).strip()
    try:
        dt.datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except ValueError:
        return None, (message or "").strip()
    return moment, match.group(2).strip()


def recall_moment(moment: Optional[str]) -> str:
    """The instant a recall is anchored to: a plain day means the end of that day."""
    if not moment:
        return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    if len(moment) == 10:
        return f"{moment}T23:59:59Z"
    parsed = dt.datetime.fromisoformat(moment.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def load_config(hermes_home: str) -> dict:
    path = Path(hermes_home) / "past.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("past.dev: %s is not valid JSON", path)
        return {}
    return value if isinstance(value, dict) else {}


class PastClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def recall(self, query: str, as_of: str, identity: str, timeout: float) -> dict:
        if len(query) > RECALL_QUERY_MAX:
            query = query[:RECALL_QUERY_MAX].rsplit(" ", 1)[0]
        body = {"query": query, "limit": RECALL_LIMIT, "maxTokens": RECALL_MAX_TOKENS,
                "queryTimestamp": as_of, "identity": identity}
        return self._post("/api/v1/recall", body, timeout)

    def _post(self, path: str, body: dict, timeout: float) -> dict:
        data = json.dumps(body).encode("utf-8")
        for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
            last = attempt == len(RETRY_DELAYS_SECONDS)
            request = urllib.request.Request(
                self._base_url + path, data=data, method="POST",
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Content-Type": "application/json", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as error:
                if (error.code == 429 or error.code >= 500) and not last:
                    time.sleep(RETRY_DELAYS_SECONDS[attempt])
                    continue
                detail = error.read().decode("utf-8", "replace")[:300]
                raise RuntimeError(f"past.dev {path} returned {error.code}: {detail}") from error
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if last:
                    raise
                time.sleep(RETRY_DELAYS_SECONDS[attempt])
        raise AssertionError("unreachable")


class PastMemoryProvider(MemoryProvider):
    def __init__(self) -> None:
        self._hermes_home = ""
        self._config: dict = {}
        self._client: Optional[PastClient] = None
        self._identity = ""
        self._session_id = ""
        self._moment: Optional[str] = None
        self._write_enabled = True
        self._spool_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "past"

    def is_available(self) -> bool:
        from hermes_constants import get_hermes_home
        config = load_config(str(get_hermes_home()))
        return bool(config.get("base_url") and config.get("api_key") and config.get("identity"))

    def unavailable_reason(self) -> str:
        return "Write base_url, api_key and identity to $HERMES_HOME/past.json"

    def initialize(self, session_id: str, **kwargs) -> None:
        from hermes_constants import get_hermes_home
        self._hermes_home = kwargs.get("hermes_home") or str(get_hermes_home())
        self._session_id = session_id
        self._config = load_config(self._hermes_home)
        self._identity = str(self._config.get("identity") or "")
        self._client = PastClient(str(self._config["base_url"]), str(self._config["api_key"]))
        # Cron, flush and subagent contexts never write: only the user's own turns are kept.
        self._write_enabled = kwargs.get("agent_context", "primary") not in {"cron", "flush", "subagent"}

    def system_prompt_block(self) -> str:
        return (
            "Your long-term memory of the user is past.dev. Call past_recall first whenever a "
            "request depends on anything from earlier conversations: people, projects, decisions, "
            "preferences, instructions, dates, numbers, commitments or exact wording. It returns "
            "dated excerpts of those conversations and what was learned from them. Ask for each "
            "fact with a short, specific query, one query per call, calling it again for each "
            "further fact, and recall again with narrower or different words if the results do not "
            "hold what you need."
        )

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        moment, _ = split_dated(message)
        if moment:
            self._moment = moment

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        if memory_is_read_only("past") or not self._write_enabled or self._client is None:
            return
        moment, text = split_dated(user_content)
        if not text and not (assistant_content or "").strip():
            return
        # The spooled record is the turn's receipt: the host pushes only what reached the spool.
        record = {
            "seed_item_id": os.environ.get("DOLPHINBENCH_SEED_ITEM_ID", "").strip() or None,
            "session_id": session_id or self._session_id,
            "moment": moment or self._moment,
            "user": text,
            "assistant": (assistant_content or "").strip(),
            "captured_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        spool = Path(self._hermes_home) / "past" / "spool.jsonl"
        payload = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        with self._spool_lock:
            spool.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(spool, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                written = os.write(descriptor, payload)
                if written != len(payload):
                    raise OSError(f"short spool write: {written}/{len(payload)} bytes")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [RECALL_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name != "past_recall":
            return tool_error(f"Unknown tool: {tool_name}")
        if self._client is None:
            return tool_error("past.dev is not configured")
        query = args.get("query")
        if query is None and isinstance(args.get("queries"), list) and len(args["queries"]) == 1:
            query = args["queries"][0]
        if isinstance(args.get("queries"), list) and len(args["queries"]) > 1:
            return tool_error("Send one query per past_recall call")
        if not isinstance(query, str) or not query.strip():
            return tool_error("Missing required parameter: query")
        try:
            page = self._client.recall(query.strip(), recall_moment(self._moment), self._identity,
                                       RECALL_TIMEOUT_SECONDS)
        except Exception as error:
            logger.warning("past.dev recall failed: %s", error)
            page = {"error": f"past.dev recall failed: {str(error)[:300]}", "results": []}
        return json.dumps({"query": query.strip(), "recall": page}, ensure_ascii=False, separators=(",", ":"))


def register(ctx) -> None:
    ctx.register_memory_provider(PastMemoryProvider())
