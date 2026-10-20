#!/bin/bash
# DolphinBench, Hermes + Luna + past.dev (self-hosted): prepare, ingest, evaluate, package. Resumable: rerun to continue.
set -u
cd /path/to/dolphinbench
set -a; . /path/to/run-config/past-dolphin-local.env; . /path/to/run-config/openrouter.env; . /path/to/run-config/judge.env; . /path/to/run-config/hermes.env; set +a
export DOLPHINBENCH_AGENT_IMAGE=sha256:bd8aafd99ba172e1842b39bbc79de23d0d61b3cafa6ea1a02e9423df74cdba78
export JUDGE_RELAY_LOG=/path/to/dolphinbench/tmp/hermes-past-full.relay/judge-relay.jsonl AGENT_RELAY_LOG=/path/to/dolphinbench/tmp/hermes-past-full.relay/agent-relay.jsonl AGENT_RELAY_TAGS=/path/to/dolphinbench/tmp/hermes-past-full.relay/tags
export PYTHONPATH=/path/to/benchmarks/dolphin
.venv/bin/python /path/to/benchmarks/dolphin/judge_relay.py & J=$!
.venv/bin/python /path/to/benchmarks/dolphin/agent_relay.py & A=$!
trap 'kill $J $A 2>/dev/null' EXIT
sleep 2
for stage in prepare ingest evaluate package; do
  echo "$(date '+%F %T') start $stage"
  case $stage in
    prepare) cmd=(.venv/bin/python -m harness.runner prepare --config hermes-past-full.yaml) ;;
    ingest) cmd=(.venv/bin/python /path/to/benchmarks/dolphin/ingest_parallel.py --config hermes-past-full.yaml --confirm-paid-calls) ;;
    evaluate) cmd=(.venv/bin/python /path/to/benchmarks/dolphin/evaluate_parallel.py --config hermes-past-full.yaml --confirm-paid-calls --workers 3) ;;
    package) cmd=(.venv/bin/python -m harness.runner package --config hermes-past-full.yaml) ;;
  esac
  "${cmd[@]}" 2>&1 | grep --line-buffered -vE "Processing request of type|DeprecationWarning|utcnow"
  rc=${PIPESTATUS[0]}
  if [ $rc -ne 0 ]; then echo "$(date '+%F %T') FAILED $stage (exit $rc)"; exit 1; fi
  echo "$(date '+%F %T') done $stage"
done
echo "$(date '+%F %T') ALL DONE"
