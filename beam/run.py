"""Run BEAM against Past's public API.

    python run.py --split 100k

The run has three phases, each over every conversation:

    1. send: create a project, register who takes part, and ingest every turn
    2. wait until Past has finished processing every project
    3. score: ask each question, answer it from what Past recalls, and score the answer

Sending everything first lets Past process all the projects at the same time. With `--reuse TAG`,
the run sends nothing and scores against the projects an earlier run filled, once Past has finished
processing them.

Environment: PAST_API_URL, PAST_MANAGEMENT_KEY (past_mk_...), OPENROUTER_API_KEY.
"""

import argparse
import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

import exabase_prompts
import llm
import scoring
from dataset import (ASSISTANT_IDENTITY, ASSISTANT_TRAITS, SPLITS, Conversation, Question,
                     build_conversation, load_rows, to_ingest_item, utc_iso)
from log import duration, log
from past_api import RECALL_LIMIT, RECALL_MAX_TOKENS, PastApi, PastApiError

HARNESS_VERSION = "2"  # 2: a failed call scores 0 instead of stopping the run

# How the work is paced: how many conversations are sent and scored at once, how many turns go in
# one push, and how often the harness asks whether Past has finished.
CONVERSATIONS_SENT_AT_ONCE = 4
CONVERSATIONS_SCORED_AT_ONCE = 4
ITEMS_PER_PUSH = 100
MAX_PUSH_BYTES = 8 * 1024 * 1024
RECALLS_AT_ONCE = 1
SETTLEMENT_POLL_SECONDS = 2
# While Past processes, a progress line every this many seconds.
PROGRESS_LOG_SECONDS = 30


class IngestionFailed(RuntimeError):
    """Past could not process a push. The run stops rather than score a conversation whose memory
    is incomplete."""


# --- Phase 1, send: a project, and who takes part -------------------------------------------------------

async def open_project(management: PastApi, api_url: str, name: str, conversation: Conversation) -> PastApi:
    """A project for one conversation.

    A real product would keep its users in one project and separate them with audiences. One
    project per conversation keeps them just as separate.
    """
    slug = await management.create_project(name)
    project = PastApi(api_url, (await management.create_project_key(slug, "beam-open"))["key"])
    log(f"project {slug} created", conversation.id)
    # The user, who the memories belong to and who asks the questions, and the assistant, an agent.
    # No audience is set, so both sides of the conversation are visible to the user.
    await project.register_identities([
        {"identity": conversation.user_identity, "traits": conversation.user_traits},
        {"identity": ASSISTANT_IDENTITY, "traits": ASSISTANT_TRAITS},
    ])
    log(f"identities registered: {conversation.user_identity} ({len(conversation.user_traits)} traits) "
        f"and {ASSISTANT_IDENTITY}",
        conversation.id)
    return project


# --- Phase 1, send: every turn -------------------------------------------------------------------

async def ingest(project: PastApi, conversation: Conversation) -> list[str]:
    """Pushes every turn, one push after another, and returns the ingestion id of each push.

    One at a time keeps the conversation's order. Other conversations send meanwhile.
    """
    items = [to_ingest_item(conversation, message) for message in conversation.messages]
    pushes = batches(items)
    log(f"ingesting {len(items)} turns in {len(pushes)} pushes", conversation.id)
    ingestion_ids = []
    turns_sent = 0
    for batch in pushes:
        receipt = await project.ingest_batch(batch, idempotency_key(batch))
        ingestion_ids.append(receipt["ingestionId"])
        turns_sent += len(batch)
        log(f"sent {turns_sent}/{len(items)} turns", conversation.id)
    return ingestion_ids


def batches(items: list[dict]) -> list[list[dict]]:
    """Up to 100 items per push, closing a push early before 8 MiB of content."""
    result, batch, batch_bytes = [], [], 0
    for item in items:
        item_bytes = len(item["content"].encode("utf-8"))
        if batch and (len(batch) == ITEMS_PER_PUSH or batch_bytes + item_bytes > MAX_PUSH_BYTES):
            result.append(batch)
            batch, batch_bytes = [], 0
        batch.append(item)
        batch_bytes += item_bytes
    if batch:
        result.append(batch)
    return result


def idempotency_key(batch: list[dict]) -> str:
    """The same batch always carries the same key, so a retried push is never ingested twice."""
    content = json.dumps(batch, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()


# --- Phase 2: wait until Past has processed everything --------------------------------------------

async def wait_until_processed(project: PastApi, ingestion_ids: list[str]) -> None:
    """Wait until Past has processed every push of the project.

    `settled` describes the whole project: no push in it is still being processed or has failed.
    So one push is enough to ask about: the last one sent. The project is
    done when that push is `completed` and the project is `settled`; by then every memory Past
    extracted is indexed and can be recalled. A push with status `failed` stops the run; so does
    `blocked`, which means nothing is left to process but some push in the project failed.
    """
    if not ingestion_ids:
        return  # a reused project whose pushes were accepted long ago (see Run.reopen)
    last_push = ingestion_ids[-1]
    while True:
        status = await project.ingestion_status(last_push)
        state = str(status.get("status", "")).lower()
        if state == "failed":
            raise IngestionFailed(f"Push {last_push} failed: {status.get('failure')}")
        if status.get("blocked"):
            raise IngestionFailed("A push in this project failed, so the project cannot finish processing")
        if state == "completed" and status.get("settled"):
            return
        await asyncio.sleep(SETTLEMENT_POLL_SECONDS)


# --- Phase 3, score: ask, answer, judge ------------------------------------------------------------------

async def answer_question(project: PastApi, conversation: Conversation, question: Question,
                          recall_slots: asyncio.Semaphore) -> dict:
    try:
        # The user asks, at the question's moment in the conversation.
        async with recall_slots:
            recalled = await project.recall(question.text, question.as_of, identity=conversation.user_identity)
        check_recalled_moment(recalled, question.as_of)

        # The answer model reads Past's response exactly as the API returned it.
        context = json.dumps(recalled, ensure_ascii=False, separators=(",", ":"))
        prompt = exabase_prompts.answer_prompt(question.text, context, question.category, question.annotations)
        answer = (await llm.complete_text(prompt)).strip()
    except (PastApiError, llm.ModelError, ValueError, httpx.HTTPError) as error:
        if llm.stops_the_run(error):
            raise
        # A recall or an answer that failed even after retries scores the question 0, and the
        # summary counts it: a failure is part of the result, never a reason to try again.
        log(f"{question.id}: failed, scored 0 ({str(error)[:160]})", conversation.id)
        return {
            "questionId": question.id,
            "conversationId": conversation.id,
            "category": question.category,
            "question": question.text,
            "asOf": question.as_of,
            "goldAnswers": question.gold_answers,
            "error": str(error)[:500],
            "judge": {"method": "failed"},
            "score": 0.0,
        }

    score, verdicts = await scoring.score_question(question, answer)
    log(f"{question.id}: {len(recalled.get('results') or [])} results recalled "
        f"({recalled.get('usedEvidenceTokens', '?')} evidence tokens), score {score:.2f}", conversation.id)
    return {
        "questionId": question.id,
        "conversationId": conversation.id,
        "category": question.category,
        "question": question.text,
        "asOf": question.as_of,
        "goldAnswers": question.gold_answers,
        "recall": recalled,
        "answer": answer,
        "judge": verdicts,
        "score": score,
    }


def check_recalled_moment(recalled: dict, asked_at: str) -> None:
    """Recall must answer as of exactly the moment it was asked, or the question is not the one we meant."""
    echoed = str(recalled.get("asOf") or "")
    try:
        echoed_utc = utc_iso(datetime.fromisoformat(echoed.replace("Z", "+00:00")))
    except ValueError:
        echoed_utc = echoed
    if echoed_utc != asked_at:
        raise RuntimeError(f"Recall answered as of {echoed!r}, but the question was asked at {asked_at!r}")


# --- The run -----------------------------------------------------------------------------------

REQUIRED_ENVIRONMENT = ("PAST_API_URL", "PAST_MANAGEMENT_KEY", "OPENROUTER_API_KEY")


@dataclass
class Sent:
    """A conversation whose turns are all in Past, with the project that holds them."""
    conversation: Conversation
    project: PastApi
    ingestion_ids: list[str]


class Run:
    """What every conversation of one run shares."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.split = args.split
        self.api_url = os.environ["PAST_API_URL"]
        self.management = PastApi(self.api_url, os.environ["PAST_MANAGEMENT_KEY"])
        self.output = Path(args.output or f"results/beam-{args.split}")
        self.results = self.output / "results.jsonl"
        # Every project's name carries the run's tag: its start time, to the second, which keeps the
        # names unique in the organization. `--reuse TAG` finds an earlier run's projects by it.
        self.reusing = bool(getattr(args, "reuse", None))
        self.tag = args.reuse if self.reusing else datetime.now(timezone.utc).strftime("%m%d-%H%M%S")
        self.temporary_keys: list[tuple[str, str]] = []  # (project slug, key id), revoked at the end
        self.sending_slots = asyncio.Semaphore(CONVERSATIONS_SENT_AT_ONCE)
        self.scoring_slots = asyncio.Semaphore(CONVERSATIONS_SCORED_AT_ONCE)
        self.recall_slots = asyncio.Semaphore(RECALLS_AT_ONCE)
        self.write_lock = asyncio.Lock()
        self.projects: list[PastApi] = []  # closed when the run ends, however it ends
        self.processed = 0
        self.scored = 0

    async def send(self, row: dict) -> Sent:
        async with self.sending_slots:
            conversation = build_conversation(row)
            log(f"sending: {len(conversation.messages)} turns, {len(conversation.questions)} questions",
                conversation.id)
            name = self.project_name(conversation.id)
            project = await open_project(self.management, self.api_url, name, conversation)
            self.projects.append(project)
            ingestion_ids = await ingest(project, conversation)
            return Sent(conversation, project, ingestion_ids)

    async def wait(self, sent: Sent, started: float) -> None:
        await wait_until_processed(sent.project, sent.ingestion_ids)
        self.processed += 1
        log(f"processed after {duration(time.monotonic() - started)}", sent.conversation.id)

    async def log_processing_progress(self, total: int, started: float) -> None:
        """A progress line every PROGRESS_LOG_SECONDS, until it is cancelled."""
        while True:
            await asyncio.sleep(PROGRESS_LOG_SECONDS)
            log(f"processing: {self.processed}/{total} conversations done after "
                f"{duration(time.monotonic() - started)}")

    async def score(self, sent: Sent, total: int) -> None:
        async with self.scoring_slots:
            conversation = sent.conversation
            started = time.monotonic()
            rows = await asyncio.gather(*(answer_question(sent.project, conversation, question, self.recall_slots)
                                          for question in conversation.questions))
            await self.save(rows)
            self.scored += 1
            mean = sum(row["score"] for row in rows) / len(rows) if rows else 0.0
            log(f"scored: mean {mean:.3f} over {len(rows)} questions in {duration(time.monotonic() - started)} "
                f"· {self.scored}/{total} conversations scored", conversation.id)

    async def save(self, rows: list[dict]) -> None:
        """A conversation's rows are written in one go, so a stopped run never leaves half of one."""
        lines = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        async with self.write_lock:
            with self.results.open("a", encoding="utf-8") as file:
                file.write(lines)

    def saved_rows(self) -> list[dict]:
        if not self.results.exists():
            return []
        return [json.loads(line) for line in self.results.read_text("utf-8").splitlines() if line.strip()]

    def project_name(self, conversation_id: str) -> str:
        return f"BEAM {self.split} {self.tag} {conversation_id}"

    async def reopen(self, row: dict, slug: str) -> Sent:
        """An earlier run's project for this conversation, with a key made for this run, and the
        conversation's last push, to wait on as a fresh run does.

        The harness does not keep push ids, so it sends the last batch again. Its idempotency key is
        a hash of the batch itself, so the key the earlier run used names this exact content, and Past
        answers with that run's push: nothing is ingested again and nothing is charged. (The batch
        would only be new if the dataset revision or the harness had changed since that run.)
        """
        conversation = build_conversation(row)
        created = await self.management.create_project_key(slug, "beam-open rescore")
        self.temporary_keys.append((slug, created["id"]))
        project = PastApi(self.api_url, created["key"])
        self.projects.append(project)
        last_batch = batches([to_ingest_item(conversation, message) for message in conversation.messages])[-1]
        try:
            receipt = await project.ingest_batch(last_batch, idempotency_key(last_batch))
        except PastApiError as error:
            # An older batch may no longer be replayable with the same idempotency key.
            # For this compatibility case, assume processing is complete; its status is
            # not verified here because the original ingestion ID is unavailable.
            if error.code != "idempotency-conflict":
                raise
            log(f"reusing project {slug}; its last push cannot be replayed, "
                "so it is taken as processed", conversation.id)
            return Sent(conversation, project, [])
        log(f"reusing project {slug}; its last push is {receipt['ingestionId']}", conversation.id)
        return Sent(conversation, project, [receipt["ingestionId"]])

    async def close(self) -> None:
        for slug, key_id in self.temporary_keys:
            await self.management.revoke_project_key(slug, key_id)
        for project in self.projects:
            await project.close()
        await self.management.close()
        await llm.close()


async def run_phases(run: Run, to_run: list[dict]) -> None:
    sent = await reopen_projects(run, to_run) if run.reusing else await send_all(run, to_run)
    await wait_for_processing(run, sent)

    log(f"phase 3/3: scoring {len(sent)} conversation(s)")
    async with asyncio.TaskGroup() as group:
        for conversation in sent:
            group.create_task(run.score(conversation, len(sent)))


async def reopen_projects(run: Run, to_run: list[dict]) -> list[Sent]:
    """Phase 1 for a rescore: the conversations are already in Past, in the projects an earlier
    run named with this tag. Nothing is ingested; the run still waits for them to be processed."""
    log(f"phase 1/3: reusing the projects of run {run.tag}; nothing is ingested")
    slugs = {project["name"]: project["slug"] for project in await run.management.list_projects()}
    missing = [str(row.get("conversation_id", "")) for row in to_run
               if run.project_name(str(row.get("conversation_id", ""))) not in slugs]
    if missing:
        raise SystemExit(f"No project from run {run.tag} for conversation {', '.join(missing)}")
    async with asyncio.TaskGroup() as group:
        reopening = [group.create_task(run.reopen(row, slugs[run.project_name(str(row.get("conversation_id", "")))]))
                     for row in to_run]
    return [task.result() for task in reopening]


async def send_all(run: Run, to_run: list[dict]) -> list[Sent]:
    log(f"phase 1/3: sending {len(to_run)} conversation(s)")
    async with asyncio.TaskGroup() as group:
        sending = [group.create_task(run.send(row)) for row in to_run]
    return [task.result() for task in sending]


async def wait_for_processing(run: Run, sent: list[Sent]) -> None:
    """No time limit: a large split takes hours, and a push Past cannot process stops the run."""
    log(f"phase 2/3: waiting until Past has processed {len(sent)} conversation(s)")
    started = time.monotonic()
    progress = asyncio.create_task(run.log_processing_progress(len(sent), started))
    try:
        async with asyncio.TaskGroup() as group:
            for conversation in sent:
                group.create_task(run.wait(conversation, started))
    finally:
        progress.cancel()


async def main_async(args: argparse.Namespace) -> None:
    missing = [name for name in REQUIRED_ENVIRONMENT if not os.environ.get(name, "").strip()]
    if missing:
        raise SystemExit(f"Set {', '.join(missing)} before running (see README.md).")

    run = Run(args)
    run.output.mkdir(parents=True, exist_ok=True)
    rows, revision = load_rows(args.split)
    if args.conversations is not None:
        rows = rows[:args.conversations]
    selected = [str(row.get("conversation_id", "")) for row in rows]
    already_scored = {row["conversationId"] for row in run.saved_rows()} & set(selected)
    to_run = [row for row in rows if str(row.get("conversation_id", "")) not in already_scored]
    log(f"BEAM {args.split} (dataset revision {revision[:12]}), run {run.tag}: {len(selected)} conversation(s), "
        f"{len(already_scored)} already scored, {len(to_run)} to run · results in {run.results}")

    # In every phase, a failure cancels the rest: a run finishes or stops, it never scores around
    # a gap. Rerunning resumes after the conversations already scored.
    try:
        if to_run:
            await run_phases(run, to_run)
    finally:
        await run.close()

    scored = [row for row in run.saved_rows() if row["conversationId"] in selected]
    summary = {
        "dataset": "BEAM",
        "split": args.split,
        "datasetRevision": revision,
        "conversations": len(selected),
        **scoring.summarize(scored),
        "answerModel": llm.MODEL,
        "judgeModel": llm.MODEL,
        "reasoningEffort": llm.REASONING_EFFORT,
        "recall": {"limit": RECALL_LIMIT, "maxTokens": RECALL_MAX_TOKENS},
        "prompts": {"source": exabase_prompts.SOURCE_URL, "sha256": exabase_prompts.SOURCE_SHA256},
        "projectTag": run.tag,
        "reusedProjects": run.reusing,
        "harnessVersion": HARNESS_VERSION,
        "finishedAt": utc_iso(datetime.now(timezone.utc)),
    }
    (run.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", "utf-8")
    print_summary(summary)


def print_summary(summary: dict) -> None:
    print()
    for category, scores in summary["categories"].items():
        print(f"  {category:<26} {scores['mean']:.3f}  ({scores['questions']} questions)")
    if summary["questions"]:
        print(f"\n  {'mean over questions':<26} {summary['microAverage']:.3f}  ({summary['questions']} questions)")
        print(f"  {'mean over categories':<26} {summary['macroAverage']:.3f}")
    print()


def positive_number(text: str) -> int:
    number = int(text)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run BEAM against Past's public API.")
    parser.add_argument("--split", choices=SPLITS, default="100k")
    parser.add_argument("--conversations", type=positive_number, help="only the first N conversations")
    parser.add_argument("--output", help="results directory (default: results/beam-<split>)")
    parser.add_argument("--reuse", metavar="TAG",
                        help="recall and score again against the projects of an earlier run, "
                             "without ingesting (TAG is that run's tag, e.g. 0923-164412)")
    return parser.parse_args()


def main() -> None:
    asyncio.run(main_async(parse_arguments()))


if __name__ == "__main__":
    main()
