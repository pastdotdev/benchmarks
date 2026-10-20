"""Running an ingestion turn again after Hermes could not complete it.

Some turns end without a usable record for reasons outside the agent's control: the model
provider returning an empty response for every one of Hermes' eight retries, or Hermes dropping a
duplicated parallel tool call, which rewrites the conversation so DolphinBench cannot export it.
The adapter and the runner then stop rather than record an incomplete turn. `execute_with_reruns`
sets that attempt aside, journal and cost included (the adapter counts every set-aside attempt's
cost), clears the runner's in-flight marker, and runs the interaction again, at most RERUNS times.
In ingestion a failed attempt's Hermes session stays in the persona's profile, beside the turn that
completes; a test runs again on a fresh copy of the frozen profile, as every test does.
"""

import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

RERUNS = 3
RECOVERABLE = ("past.dev's plugin kept nothing", "no complete recorded response", "Hermes ingestion",
               "Conversation context was rewritten", "Submission recording failed", "Response truncated",
               "different user message or conversation boundary")


def set_aside(runner, persona: str, item_id: str, reason: str, phase: str = "ingestion") -> None:
    home = runner.directory / "hermes"
    target = home / "filtered" / phase / persona / f"{item_id}-{uuid.uuid4().hex[:8]}"
    target.mkdir(parents=True)
    journal = home / "attempts" / phase / persona / f"{item_id}.json"
    if journal.exists():
        os.replace(journal, target / "attempts.json")
    for cost in (home / "costs" / phase / persona).glob(f"{item_id}-*.json"):
        os.replace(cost, target / cost.name)
    if phase == "tests":
        for profile in (home / "profiles").glob(f"{persona}-test-{item_id}-a*"):
            shutil.rmtree(profile)
        trace = home / "test-traces" / persona / f"{item_id}.jsonl"
        if trace.exists():  # the adapter refuses to run a test again while its saved trace exists
            os.replace(trace, target / trace.name)
    (target / "filtered.json").write_text(json.dumps({"reason": reason[:2000], "at": time.time()}))
    for suffix in (".inflight", ".returned"):
        runner._path(phase, persona, item_id).with_suffix(suffix).unlink(missing_ok=True)


HERMES_NUDGE = "[System: Continue now."


def cut_at_hermes_nudge(messages: list) -> list | None:
    """The record up to the continuation prompt Hermes adds on its own, which DolphinBench's format
    cannot hold: the user's message and the agent's answer to it. None if there is none."""
    for index, message in enumerate(messages):
        if (message.get("role") == "user" and str(message.get("content") or "").startswith(HERMES_NUDGE)
                and index > 0 and messages[index - 1].get("role") == "assistant"
                and not messages[index - 1].get("tool_calls")):
            return messages[:index]
    return None


def execute_cut_at_nudge(runner, persona: str, spec: dict, phase: str, item_id: str):
    """Last resort for a turn Hermes always continues with its own prompt: run it once more and
    record it up to that prompt. Each such turn is listed in hermes/nudge-cut.jsonl."""
    adapter = runner.adapter
    original = adapter.run_interaction

    def run_interaction(request):
        record = original(request)
        cut = cut_at_hermes_nudge(record.messages)
        if cut is not None:
            record.messages[:] = cut
            with (runner.directory / "hermes" / "nudge-cut.jsonl").open("a") as log:
                log.write(json.dumps({"phase": phase, "persona": persona, "id": item_id, "at": time.time()}) + "\n")
        return record

    adapter.run_interaction = run_interaction
    try:
        return runner._execute(phase, persona, spec)
    finally:
        adapter.run_interaction = original


def execute_with_reruns(runner, persona: str, spec: dict, phase: str = "ingestion"):
    item_id = str(spec["id"]).zfill(6 if phase == "ingestion" else 3)
    for rerun in range(RERUNS + 1):
        try:
            return runner._execute(phase, persona, spec)
        except Exception as error:
            if rerun == RERUNS and "different user message" in str(error):
                set_aside(runner, persona, item_id, str(error), phase)
                return execute_cut_at_nudge(runner, persona, spec, phase, item_id)
            if rerun == RERUNS or not any(text in str(error) for text in RECOVERABLE):
                raise
            print(f"{persona}/{item_id}: turn not completed ({str(error)[:200]}), running it again",
                  file=sys.stderr)
            set_aside(runner, persona, item_id, str(error), phase)


def _conversation_calls(messages: list) -> list:
    """Each app call as the agent sent it, through Hermes' tool_call wrapper too."""
    calls = []
    for message in messages:
        for call in message.get("tool_calls") or []:
            name, arguments = call.get("name") or (call.get("function") or {}).get("name"), call.get("arguments")
            if arguments is None:
                arguments = (call.get("function") or {}).get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    continue
            if not isinstance(arguments, dict) or not isinstance(name, str):
                continue
            if name == "tool_call" and isinstance(arguments.get("name"), str):
                name, arguments = arguments["name"], arguments.get("arguments") or {}
            for prefix in ("mcp__dolphinbench_apps__", "mcp_dolphinbench_apps_"):
                name = name[len(prefix):] if name.startswith(prefix) else name
            calls.append((name, arguments))
    return calls


def drop_unsent_defaults(directory: Path, persona: str, item_id: str, record) -> None:
    """DolphinBench's app server logs each call with the arguments it ran, its own defaults
    included: an email sent without cc is logged with "cc": null, a pull-request list asked for
    without a status is logged with "status": "open". The grader then finds no conversation call
    with those exact arguments. When an app call matches none of the agent's calls exactly, but
    every argument the agent sent (leaving out nulls) has the same value in it, it is recorded with
    the arguments the agent sent; each such call is listed in hermes/app-call-defaults.jsonl."""
    def key(tool, args):
        return json.dumps([tool, args], sort_keys=True)
    sent = _conversation_calls(record.messages)
    used = [False] * len(sent)
    calls = [call for call in record.app_calls or [] if isinstance(call.get("args"), dict)]
    pending = []
    for call in calls:  # exact matches first, as the grader takes them
        index = next((i for i, (tool, args) in enumerate(sent)
                      if not used[i] and key(tool, args) == key(call.get("tool"), call["args"])), None)
        if index is None:
            pending.append(call)
        else:
            used[index] = True
    for call in pending:
        for i, (tool, args) in enumerate(sent):
            given = {name: value for name, value in args.items() if value is not None}
            if (used[i] or tool != call.get("tool")
                    or any(name not in call["args"] or call["args"][name] != value for name, value in given.items())):
                continue
            used[i] = True
            logged = call["args"]
            call["args"] = args
            with (directory / "hermes" / "app-call-defaults.jsonl").open("a") as log:
                log.write(json.dumps({"persona": persona, "id": item_id, "tool": call.get("tool"), "logged": logged, "sent": args, "at": time.time()}) + "\n")
            break
    available = {}
    for tool, args in sent:
        available[key(tool, args)] = available.get(key(tool, args), 0) + 1
    for call in record.app_calls or []:
        if not isinstance(call.get("args"), dict) or available.get(key(call.get("tool"), call["args"]), 0) < 1:
            # Raised before the record is saved, so the test is set aside and runs again.
            raise RuntimeError("Submission recording failed: app call has no matching conversation tool call "
                               f"({call.get('tool')} {json.dumps(call.get('args'))[:200]})")
        available[key(call.get("tool"), call["args"])] -= 1


def await_processed(adapter, persona: str, limit_seconds: float = 6 * 3600) -> None:
    """Before a persona's memory is frozen: wait until past.dev has processed every push of its
    project, asking it to retry any push that failed (it retries a failed push on request), so a
    failure inside past.dev, such as a full disk on the host, does not stop the run."""
    harness = adapter.past
    client, _ = harness._project(persona)
    receipts = json.loads((harness.state / "receipts" / f"{persona}.json").read_text())
    retried: dict[str, int] = {}
    deadline = time.monotonic() + limit_seconds
    while True:
        states = [(receipt["ingestionId"], client.ingestion_status(receipt["ingestionId"])) for receipt in receipts]
        failed = [ingestion for ingestion, status in states if str(status.get("status", "")).lower() == "failed"]
        last = states[-1][1]
        if not failed and str(last.get("status", "")).lower() == "completed" and last.get("settled"):
            return
        for ingestion in failed:
            if retried.get(ingestion, 0) >= 3:
                raise RuntimeError(f"{persona}: past.dev push {ingestion} failed after 3 retries")
            client._call("POST", f"/api/v1/ingest/{ingestion}/retry")
            retried[ingestion] = retried.get(ingestion, 0) + 1
            print(f"{persona}: past.dev push {ingestion} failed, retry {retried[ingestion]}", file=sys.stderr)
        if time.monotonic() > deadline:
            raise RuntimeError(f"{persona}: past.dev did not finish processing within {limit_seconds / 3600:.0f} h")
        time.sleep(30)


def freeze_disclosing(adapter, persona: str) -> dict:
    """The adapter's freeze, with the pushes past.dev completed without one memory listed in the
    checkpoint instead of stopping the run. During this run the hosted entailment service failed
    for hours and past.dev completed those pushes with no memory and no error; their data points
    are stored and searchable, and the result discloses the list."""
    from harness.adapters.hermes import HermesAdapter
    from harness.durable_json import save_json
    from past_harness import digest, now_iso, wait_until_processed
    harness = adapter.past
    harness._flush(persona)
    client, slug = harness._project(persona)
    receipts = json.loads((harness.state / "receipts" / f"{persona}.json").read_text())
    wait_until_processed(client, receipts[-1]["ingestionId"])
    empty = [index + 1 for index, receipt in enumerate(receipts)
             if not any(int(item.get("memories") or 0) for item in client.explain(receipt["ingestionId"]).get("items") or [])]
    harness._mark_phase(persona, "ingestion_end", replace=True)
    past = {"provider": "past.dev", "project": slug, "pushes": len(receipts),
            "items": sum(len(receipt["items"]) for receipt in receipts),
            "receiptsSha256": digest(receipts), "lastIngestionId": receipts[-1]["ingestionId"],
            "memoryCount": harness._memory_count(slug), "frozenAt": now_iso(),
            "pushesWithoutMemories": empty}
    save_json(harness.state / "checkpoints" / f"{persona}.json", past)
    if empty:
        print(f"{persona}: {len(empty)} past.dev push(es) completed without memories, disclosed: {empty}", file=sys.stderr)
    return {"hermes": HermesAdapter.freeze(adapter, persona), "past": past}


def reconcile(directory: Path, persona: str, item_id: str, reason: str) -> None:
    """For a run stopped on such a turn: set it aside so that resuming runs it again."""
    class Runner:
        pass
    runner = Runner()
    runner.directory = directory
    runner._path = lambda phase, p, i: directory / phase / p / f"{i}.json"
    set_aside(runner, persona, item_id, reason)


if __name__ == "__main__":
    reconcile(Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4])


READ_ONLY_PREFIXES = ("get_", "list_", "search_", "internal_search", "check_", "read_", "find_")


def reconcile_ingestion_app_calls(directory: Path) -> dict:
    """Ingestion records are saved before packaging checks them, and a frozen memory cannot be
    rebuilt, so the app server's log is reconciled with the conversation in place. Three cases
    are handled, each one listed in hermes/app-call-defaults.jsonl with the original record kept
    under hermes/app-call-originals: the server's own defaults (as for tests); an argument the
    server does not know, which it ignores, so the call ran as if it had not been sent; and a
    read-only call the server ran that the conversation does not show (Hermes drops a duplicated
    parallel call from the conversation after the server ran it), which is removed."""
    from collections import Counter
    from harness.submission import grading_calls

    def key(tool, args):
        return json.dumps([tool, args], sort_keys=True)

    counts = Counter()
    log = (directory / "hermes" / "app-call-defaults.jsonl").open("a")
    for path in sorted((directory / "ingestion").glob("*/*.json")):
        evidence = json.loads(path.read_text())
        row = evidence.get("row")
        if not row:
            continue
        try:
            grading_calls(row, str(path))
            continue
        except Exception:
            pass
        original = json.loads(json.dumps(row.get("app_calls")))
        sent = _conversation_calls(row["messages"])
        used = [False] * len(sent)
        kept = []
        changes = []
        for call in row.get("app_calls") or []:
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            index = next((i for i, (tool, given) in enumerate(sent)
                          if not used[i] and key(tool, given) == key(call.get("tool"), args)), None)
            if index is None:
                for i, (tool, given) in enumerate(sent):
                    if used[i] or tool != call.get("tool"):
                        continue
                    shared = [name for name in given if name in args]
                    if (all(given[name] == args[name] or given[name] is None for name in shared)
                            and all(value is None or name not in given or given[name] is None
                                    or given[name] == value for name, value in args.items())):
                        index = i
                        changes.append({"tool": tool, "logged": args, "sent": given})
                        call = {**call, "args": given}
                        break
            if index is None and str(call.get("tool", "")).startswith(READ_ONLY_PREFIXES):
                changes.append({"tool": call.get("tool"), "logged": args, "removed": "read-only call missing from the conversation"})
                continue
            if index is not None:
                used[index] = True
            kept.append(call)
        candidate = dict(row, app_calls=kept)
        try:
            grading_calls(candidate, str(path))
        except Exception as error:
            counts["unresolved"] += 1
            print(f"unresolved: {path.relative_to(directory)}: {str(error)[:200]}", file=sys.stderr)
            continue
        backup = directory / "hermes" / "app-call-originals" / path.relative_to(directory)
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            backup.write_text(json.dumps({"app_calls": original}))
        evidence["row"] = candidate
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(evidence, ensure_ascii=False))
        os.replace(tmp, path)
        log.write(json.dumps({"phase": "ingestion", "record": str(path.relative_to(directory)), "changes": changes, "at": time.time()}) + "\n")
        counts["reconciled"] += 1
    log.close()
    return dict(counts)
