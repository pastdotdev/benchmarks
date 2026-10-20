"""Regrade the reported Hermes tests whose app calls were reconciled, on the app server's own log.

The rerun recorded 231 app calls in 183 tests with the arguments the agent sent instead of the ones
the server logged (hermes/app-call-defaults.jsonl, the lines after the original run's). This puts
each one back as the server logged it and grades the test again with DolphinBench's grader. A judge
check whose prompt is unchanged replays its recorded verdict; any other goes to the official judge
(gpt-5.6-sol, medium, through judge_relay.py), so set OPENROUTER_API_KEY.

    python scripts/regrade_server_logged.py --release ~/dolphinbench --run tmp/exp-toolsets-600 \
        --original tmp/hermes-past-full --relay judge_relay.py --out regrade-server-logged
"""
import argparse
import copy
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path


def start_relay(relay_script: Path, out: Path) -> subprocess.Popen:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    os.environ.update(AZURE_OPENAI_API_KEY=secrets.token_hex(16), AZURE_OPENAI_ENDPOINT=f"http://127.0.0.1:{port}")
    relay = subprocess.Popen([sys.executable, str(relay_script)],
                             env={**os.environ, "JUDGE_RELAY_PORT": str(port),
                                  "JUDGE_RELAY_LOG": str(out / "judge-relay.jsonl")})
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return relay
        except OSError:
            if relay.poll() is not None:
                sys.exit("judge_relay.py did not start")
            time.sleep(0.2)
    relay.terminate()
    sys.exit("judge_relay.py did not start")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", type=Path, required=True)
    ap.add_argument("--run", type=Path, required=True, help="the rerun's folder (exp-toolsets-600)")
    ap.add_argument("--original", type=Path, required=True, help="the original run's folder (hermes-past-full)")
    ap.add_argument("--relay", type=Path, required=True, help="judge_relay.py")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    if not os.environ.get("OPENROUTER_API_KEY"):
        sys.exit("Set OPENROUTER_API_KEY")

    before = (args.original / "hermes/app-call-defaults.jsonl").read_text().splitlines()
    lines = (args.run / "hermes/app-call-defaults.jsonl").read_text().splitlines()
    if lines[:len(before)] != before:
        sys.exit("The rerun's change log does not start with the original run's")
    changes = defaultdict(list)
    for line in lines[len(before):]:
        entry = json.loads(line)
        changes[entry["persona"], entry["id"]].append(entry)

    args.out.mkdir(parents=True, exist_ok=True)
    relay = start_relay(args.relay, args.out)
    # The judge reads its endpoint when it is imported, so the relay starts first.
    sys.path.insert(0, str(args.release))
    import yaml
    from graders.judge_recording import replay_judge
    from graders.mechanical import grade_tool_trace
    judge = {"model": "gpt-5.6-sol", "reasoning_effort": "medium"}
    rows = []
    try:
        for (persona, test_id), entries in sorted(changes.items()):
            record = json.loads((args.run / "tests" / persona / f"{test_id}.json").read_text())
            calls = copy.deepcopy(record["row"]["app_calls"])
            restored = 0
            for entry in entries:
                call = next((c for c in calls if c.get("tool") == entry["tool"] and c.get("args") == entry["sent"]
                             and not c.get("_restored")), None)
                if call is None:
                    continue  # logged in an attempt that was set aside and run again
                call["args"], call["_restored"] = entry["logged"], True
                restored += 1
            for call in calls:
                call.pop("_restored", None)
            if not restored:
                continue
            spec = yaml.safe_load((args.release / "tests" / persona / f"{test_id}.yaml").read_text())
            config = copy.deepcopy(spec["grade"]["config"])
            config["raise_on_judge_error"] = True
            grade = json.loads((args.run / "grades" / persona / f"{test_id}.json").read_text())
            recorded = [m for check in grade["checks"] for m in check.get("messages", [])]
            source = "replayed"
            try:
                with replay_judge(recorded, judge):
                    result = grade_tool_trace(calls, config, test_message=spec["test"])
            except ValueError as error:
                if "Unused judge messages" not in str(error):
                    source = "judged"
                    result = grade_tool_trace(calls, config, test_message=spec["test"])
            passed = all(d["ok"] for d in result["details"])
            was = all(c["passed"] for c in grade["checks"])
            rows.append({"persona": persona, "id": test_id, "calls_restored": restored, "reported": was,
                         "server_logged": passed, "verdicts": source,
                         "checks": [d["ok"] for d in result["details"]]})
            print(f"{persona} {test_id}: reported {'pass' if was else 'fail'}, "
                  f"server-logged {'pass' if passed else 'fail'} ({source})", flush=True)
    finally:
        relay.terminate()
    (args.out / "tests.json").write_text(json.dumps(rows, indent=1) + "\n")
    cost = sum(json.loads(l).get("cost_usd", 0) or 0
               for l in (args.out / "judge-relay.jsonl").read_text().splitlines()) \
        if (args.out / "judge-relay.jsonl").exists() else 0.0
    flipped = [r for r in rows if r["reported"] != r["server_logged"]]
    print(f"\n{len(rows)} tests regraded ({sum(r['verdicts'] == 'replayed' for r in rows)} replayed, "
          f"{sum(r['verdicts'] == 'judged' for r in rows)} judged again); "
          f"reported passes {sum(r['reported'] for r in rows)}, server-logged passes "
          f"{sum(r['server_logged'] for r in rows)}; {len(flipped)} changed: "
          + ", ".join(f"{r['persona']} {r['id']}" for r in flipped) + f"; judge cost ${cost:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
