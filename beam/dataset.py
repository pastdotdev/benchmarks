"""BEAM, shaped the way a chat app would send it to Past.

Each BEAM row is one long conversation between a user and an assistant, plus the questions the
benchmark asks about it. This module turns a row into:

- the messages an app would ingest: who wrote each turn, when, and in which session;
- the questions, each with the moment it is asked.

The question annotations (rubrics, hints) stay on this side. None of them is ever sent to Past;
they exist only because ExaBase's answer and judge prompts read them.
"""

import ast
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Where each split lives on Hugging Face.
HF_SOURCES = {
    "100k": ("Mohammadta/BEAM", "100K"),
    "500k": ("Mohammadta/BEAM", "500K"),
    "1m": ("Mohammadta/BEAM", "1M"),
    "10m": ("Mohammadta/BEAM-10M", "10M"),
}
SPLITS = list(HF_SOURCES)

CATEGORIES = [
    "abstention",
    "contradiction_resolution",
    "event_ordering",
    "information_extraction",
    "instruction_following",
    "knowledge_update",
    "multi_session_reasoning",
    "preference_following",
    "summarization",
    "temporal_reasoning",
]

# A question's reference answer is the first of these fields it has...
GOLD_FIELDS = ["ideal_response", "answer", "expected_answer", "expected", "gold_answer", "reference"]
# ...and when that one is blank, the first of these that is set.
FALLBACK_GOLD_FIELDS = ["ideal_answer", "ideal_summary", "expected_compliance"]

# The assistant writes half of every conversation. It is an agent in the project's directory.
ASSISTANT_IDENTITY = "assistant"
ASSISTANT_TRAITS = {"kind": "agent"}

# BEAM dates each session with a day, like "March-15-2024". Its turns are placed at noon UTC.
DATE_FORMATS = ("%B-%d-%Y", "%B %d, %Y", "%B %d %Y", "%Y-%m-%d")

# Past accepts trait values up to 2,000 characters.
MAX_TRAIT_VALUE = 2_000

CACHE_DIR = Path(os.environ.get("BEAM_OPEN_CACHE", Path.home() / ".cache" / "beam-open"))


@dataclass
class Message:
    message_id: str
    session_id: str
    ordinal: int  # position in the conversation, from 0
    role: str  # "user" or "assistant"
    author: str  # the user's name, or "Assistant"
    identity: str  # the Past identity that wrote it
    content: str
    timestamp: datetime
    turn_id: str | None  # BEAM's own turn id, used only to date knowledge_update questions


@dataclass
class Question:
    id: str
    category: str
    text: str
    gold_answers: list[str]
    annotations: dict  # the question fields ExaBase's prompts read (rubric, hints)
    as_of: str  # the moment the question is asked, sent to recall as queryTimestamp


@dataclass
class Conversation:
    id: str
    title: str
    user_identity: str  # the user's Past identity: their name and the conversation ("craig-baker-3")
    user_traits: dict[str, str]
    messages: list[Message]
    questions: list[Question]


# --- Loading ---------------------------------------------------------------------------------

def load_rows(split: str) -> tuple[list[dict], str]:
    """All rows of a split and the dataset revision they come from.

    The first call downloads the split at its current revision and caches it; later calls read
    the cache, so a run always knows exactly which data it scored.
    """
    name, hf_split = HF_SOURCES[split]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    rows_file = CACHE_DIR / f"{split}.json"
    revision_file = CACHE_DIR / f"{split}.revision"
    if rows_file.exists() and revision_file.exists():
        return json.loads(rows_file.read_text("utf-8")), revision_file.read_text().strip()

    from datasets import load_dataset
    from huggingface_hub import HfApi

    revision = HfApi().dataset_info(name).sha
    print(f"Downloading BEAM {split} ({name}, split {hf_split}, revision {revision})")
    rows = [dict(row) for row in load_dataset(name, split=hf_split, revision=revision)]
    rows_file.write_text(json.dumps(rows), "utf-8")
    revision_file.write_text(revision)
    return rows, revision


def build_conversation(row: dict) -> Conversation:
    messages = build_messages(row)
    return Conversation(
        id=conversation_id(row),
        title=conversation_title(row),
        user_identity=user_identity(row),
        user_traits=user_traits(row),
        messages=messages,
        questions=build_questions(row, messages),
    )


def to_ingest_item(conversation: Conversation, message: Message) -> dict:
    """One turn as an app would push it: its own id, its author and its time."""
    return {
        "id": message.message_id,
        "content": message.content,
        "label": conversation.title,
        "timestamp": message.timestamp.isoformat(),
        "identity": message.identity,
        "metadata": {
            "role": message.role,
            "author": message.author,
            "speaker": message.author,
        },
    }


def utc_iso(moment: datetime) -> str:
    """A moment as Past writes it: UTC, ending in Z."""
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# --- The conversation and its user -----------------------------------------------------------

def conversation_id(row: dict) -> str:
    return str(row.get("conversation_id", ""))


def conversation_title(row: dict) -> str:
    seed = row.get("conversation_seed") or {}
    return str(seed.get("title") or f"Conversation {conversation_id(row)}")


def user_profile(row: dict) -> str:
    """The profile paragraph BEAM gives each user: "• Name: Craig Baker  • Age: 34 ..."."""
    profile = row.get("user_profile") or {}
    return str(profile.get("user_info") or "") if isinstance(profile, dict) else ""


def user_name(row: dict) -> str:
    match = re.search(r"Name:\s*(.+)", user_profile(row))
    return match.group(1).strip() if match else "User"


def user_identity(row: dict) -> str:
    """The user's identity id: their name plus the conversation id, since BEAM reuses names.
    "Craig Baker" in conversation 3 is "craig-baker-3"; without a name, "user-3"."""
    name = re.sub(r"[^a-z0-9]+", "-", user_name(row).lower()).strip("-")
    return f"{name or 'user'}-{conversation_id(row)}"


def user_traits(row: dict) -> dict[str, str]:
    """Each "• Key: value" line of the profile becomes a trait; the whole paragraph is `profile`."""
    profile = user_profile(row)
    traits = {}
    for key, value in re.findall(r"•\s*([A-Za-z ]+?):\s*([^\n•]+)", profile):
        traits[key.strip().lower().replace(" ", "_")] = value.strip()[:MAX_TRAIT_VALUE]
    if profile.strip():
        traits["profile"] = profile.strip()[:MAX_TRAIT_VALUE]
    return traits


# --- Messages --------------------------------------------------------------------------------

def walk_turns(chat: list):
    """Every turn of a conversation, in order, as (message id suffix, session id, turn).

    The standard splits hold a list of sessions, each a list of turns. The 10M split holds plans
    of batches of turn groups. A few rows hold turns directly.
    """
    for outer, item in enumerate(chat):
        if isinstance(item, list):  # a session
            for t, turn in enumerate(item):
                if is_turn(turn):
                    yield f"s{outer:03d}_t{t:04d}", f"s{outer:03d}", turn
        elif is_turn(item):  # a turn on its own
            yield f"f{outer:06d}", "flat", item
        elif isinstance(item, dict):  # a 10M plan: {"plan-1": [batch, ...]}
            for plan_name, batches in item.items():
                if not isinstance(batches, list):
                    continue
                number = re.search(r"(\d+)$", str(plan_name))
                plan = f"{int(number.group(1)):03d}" if number else f"{outer:03d}"
                for b, batch in enumerate(batches):
                    groups = batch.get("turns") if isinstance(batch, dict) else None
                    if not isinstance(groups, list):
                        continue
                    for g, group in enumerate(groups):
                        for t, turn in enumerate(group if isinstance(group, list) else [group]):
                            if is_turn(turn):
                                yield f"p{plan}_b{b:04d}_g{g:04d}_t{t:04d}", f"p{plan}-b{b:04d}", turn


def is_turn(item) -> bool:
    return isinstance(item, dict) and "role" in item


def parse_date(value) -> datetime | None:
    """A BEAM date ("March-15-2024", "March 15th, 2024", ...) at noon UTC, or None."""
    text = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", str(value or "").strip())
    for pattern in DATE_FORMATS if text else ():
        try:
            return datetime.strptime(text, pattern).replace(hour=12, tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def fill_missing_dates(dates: list, step: timedelta, conversation: str) -> list[datetime]:
    """A missing date takes the previous known date plus `step`, or else the next one minus a day."""
    for index, date in enumerate(dates):
        if date is not None:
            continue
        previous = next((dates[i] for i in range(index - 1, -1, -1) if dates[i] is not None), None)
        following = next((d for d in dates[index + 1:] if d is not None), None)
        if previous is not None:
            dates[index] = previous + step
        elif following is not None:
            dates[index] = following - timedelta(days=1)
        else:
            raise ValueError(f"BEAM conversation {conversation} has no usable date")
    return dates


def session_date(session: list) -> datetime | None:
    """A session's date is the first time anchor any of its turns carries."""
    anchor = next((turn.get("time_anchor") for turn in session
                   if isinstance(turn, dict) and turn.get("time_anchor")), None)
    return parse_date(anchor)


def build_messages(row: dict) -> list[Message]:
    """One message per turn that has content.

    Turns share their session's day. Each turn then adds its position in microseconds, so the
    conversation's order is exact and a question can be asked right after one turn and before
    the next.
    """
    conversation = conversation_id(row)
    chat = row.get("chat") or []
    turns = list(walk_turns(chat))
    if not turns:
        raise ValueError(f"BEAM conversation {conversation} contains no turns")

    # The day of each turn. Standard splits date whole sessions, and a session with no date comes an
    # hour after the one before. The other splits date each turn, and a turn with no date shares
    # the previous turn's.
    sessions = [item for item in chat if isinstance(item, list)]
    if sessions:
        session_dates = fill_missing_dates([session_date(s) for s in sessions], timedelta(hours=1), conversation)
        # In these splits every item of the chat is a session, so session "s003" is sessions[3].
        days = [session_dates[int(session_id.removeprefix("s"))] for _, session_id, _ in turns]
    else:
        days = fill_missing_dates([parse_date(turn.get("time_anchor")) for _, _, turn in turns],
                                  timedelta(0), conversation)

    name = user_name(row)
    identity = user_identity(row)
    messages = []
    for index, (suffix, session_id, turn) in enumerate(turns):
        content = str(turn.get("content") or "").strip()
        if not content:
            continue
        role = str(turn.get("role") or "user").strip().lower()
        ordinal = len(messages)
        messages.append(Message(
            message_id=f"{conversation}_{suffix}",
            session_id=session_id,
            ordinal=ordinal,
            role=role,
            author=name if role == "user" else "Assistant",
            identity=identity if role == "user" else ASSISTANT_IDENTITY,
            content=content,
            timestamp=days[index] + timedelta(microseconds=ordinal),
            turn_id=str(turn["id"]) if turn.get("id") is not None else None,
        ))
    if not messages:
        raise ValueError(f"BEAM conversation {conversation} contains no turns with content")
    return messages


# --- Questions -------------------------------------------------------------------------------

def build_questions(row: dict, messages: list[Message]) -> list[Question]:
    """The conversation's questions, in category order.

    Every question is asked at the end of the conversation, except knowledge_update questions:
    they ask for a value that changed, and are asked as of the update BEAM marks.
    """
    conversation = conversation_id(row)
    end_of_conversation = max(message.timestamp for message in messages)
    turn_times: dict[str, list[datetime]] = {}
    for message in messages:
        if message.turn_id is not None:
            turn_times.setdefault(message.turn_id, []).append(message.timestamp)

    questions = []
    by_category = probing_questions(row)
    for category in CATEGORIES:
        for index, item in enumerate(by_category.get(category, [])):
            text = item.get("question", "")
            if not text:
                continue
            asked_at = end_of_conversation
            if category == "knowledge_update":
                update_times = [time for turn in updated_turns(item) for time in turn_times.get(turn, [])]
                asked_at = max(update_times, default=end_of_conversation)
            questions.append(Question(
                id=f"{conversation}_{category}_{index}",
                category=category,
                text=text,
                gold_answers=gold_answers(item),
                annotations=annotations(category, item),
                as_of=utc_iso(asked_at),
            ))
    return questions


def probing_questions(row: dict) -> dict:
    """BEAM stores a row's questions as a dict of category → questions, sometimes as a string."""
    value = row.get("probing_questions", {})
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            try:
                value = ast.literal_eval(value)
            except Exception:
                value = {}
    return value if isinstance(value, dict) else {}


def gold_answers(item: dict) -> list[str]:
    """The reference answer: the first answer field the question has, or when that one is blank,
    the first fallback field that is set. A question may have none."""
    first = next((item[field] for field in GOLD_FIELDS if field in item), None)
    answer = "" if first is None else str(first)
    if not answer.strip():
        fallback = next((str(item[field]) for field in FALLBACK_GOLD_FIELDS if item.get(field) is not None), "")
        if fallback:
            answer = fallback
    return [answer] if answer else []


def annotations(category: str, item: dict) -> dict:
    """The question fields ExaBase's prompts read: the rubric, plus each category's hints."""
    result: dict = {}
    rubric = item.get("rubric")
    if rubric:
        result["rubric"] = rubric if isinstance(rubric, list) else [str(rubric)]
    if category == "abstention":
        result["why_unanswerable"] = item.get("why_unanswerable", "")
    elif category == "instruction_following":
        result["instruction_being_tested"] = item.get("instruction_being_tested", "")
        result["compliance_indicators"] = item.get("compliance_indicators", [])
    elif category == "preference_following":
        result["preference_being_tested"] = item.get("preference_being_tested", "")
        result["compliance_indicators"] = item.get("compliance_indicators", [])
    elif category == "temporal_reasoning":
        result["time_points"] = item.get("time_points", [])
        result["calculation_required"] = item.get("calculation_required", "")
    elif category == "event_ordering":
        result["ordering_tested"] = item.get("ordering_tested", [])
    return result


def updated_turns(item: dict) -> list[str]:
    """The turn ids BEAM cites as the update in a knowledge_update question."""
    sources = item.get("source_chat_ids")
    if not isinstance(sources, dict):
        return []

    def flatten(value):
        if isinstance(value, (list, tuple)):
            for inner in value:
                yield from flatten(inner)
        elif value is not None:
            yield str(value)

    return list(dict.fromkeys(flatten(sources.get("updated_info"))))
