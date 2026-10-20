# Run scripts

The scripts and run files of the two runs, as they ran, except `full_run.sh`, which is its last
version: it was edited on 2026-10-03 at 16:05 CDT, after the original run's first three launches,
and the earlier version is not kept. Paths on the machine that ran them are replaced by
placeholders:

| Placeholder | Stands for |
|---|---|
| `/path/to/dolphinbench` | The DolphinBench checkout at commit `81cb6f8`, where every stage ran and wrote its output under `tmp/` |
| `/path/to/benchmarks/dolphin` | This folder |
| `/path/to/run-config` | A private folder holding the env files below and the experiment helpers |

| File | |
|---|---|
| `full_run.sh` | The original Hermes + past.dev run: prepare, ingest (`ingest_parallel.py`), evaluate (`evaluate_parallel.py`, three workers) and package, resumable by running it again. It produced the ingestion and the first test pass (363 / 600). Its package stage stopped because the ingestion evidence exceeds DolphinBench's 512 MiB JSON limit. |
| `run_toolsets_600.sh` | The second test pass (489 / 600), on a clone of that run |
| `run_subset.py` | Runs a chosen set of tests on a clone of a finished run |
| `regrade_server_logged.py` | Written after the runs: grades the second pass's 183 tests with reconciled app calls again on the app server's own log (488 / 600) |
| `reextract.py` | Written after the runs: extracts again the pushes the entailment outage left without memories, one persona at a time, by setting each push back to Degraded in Pilot's database and retrying it through the API with a temporary project key |
| `reextract-alex.txt`, `reextract-riley.txt` | The 59 pushes it retried. Alex's push 72 had been replaced by a revision push before the rerun, so its list names that revision instead. |
| `run_reextract_ar.sh` | Alex's and Riley's 400 tests again after the re-extraction (490 / 600 with Morgan's 173), on a clone of the second pass; the same launcher as `run_toolsets_600.sh` but for the test list and the relay folder |
| `exp-reextract-ar.yaml` | That rerun's run file |
| `hermes-past-full.yaml` | The original Hermes run's run file |
| `exp-toolsets-600.yaml` | The second test pass's run file |
| `past-full.yaml` | The own agent's run file |

## Env files

Both shell scripts source four env files, which hold keys and stay private:

| File | Variables |
|---|---|
| `past-dolphin-local.env` | `PAST_API_URL` (the local Pilot), `PAST_MANAGEMENT_KEY` |
| `openrouter.env` | `OPENROUTER_API_KEY` |
| `judge.env` | `AZURE_OPENAI_API_KEY` (a local password between the runner and `judge_relay.py`), `AZURE_OPENAI_ENDPOINT` (`judge_relay.py`'s address) |
| `hermes.env` | `AZURE_FOUNDRY_API_KEY` (a local password between Hermes and `agent_relay.py`), `AZURE_OPENAI_BASE_URL` (`agent_relay.py` as the agent's container reaches it), `PAST_AGENT_API_URL` (past.dev as the container reaches it), `DOLPHINBENCH_HERMES_SOURCE` (the Hermes checkout), `DOLPHINBENCH_DOCKER_CONTEXT=desktop-linux`, `HERMES_MEMORY_SYNC_DRAIN_TIMEOUT_SECONDS=30`, `BUILDX_CONFIG` |

`evaluate_parallel.py` also reads a worker count from `~/.dolphin-hermes/evaluate-workers`, which
held 3, the same as `full_run.sh` passes.

## The second test pass

- **Clone.** `tmp/exp-toolsets-600` is an APFS clone of `tmp/hermes-past-full` after its first
  test pass, with that pass's test records, grades and traces removed. Each persona's frozen
  profile (`hermes/profiles/<persona>-frozen/config.yaml`) has `toolsets` and `platform_toolsets`
  set to `dolphinbench-apps`, `memory` and `session_search`. The ingestion records, checkpoints and
  past.dev projects are the original run's.
- **Launch.** From `/path/to/dolphinbench`, with `EXP_ARMS=toolsets-600` (the arm the log names)
  and `EXP_TOOLSETS=dolphinbench-apps,memory,session_search` in the environment. `run_subset.py`
  sets DolphinBench's `harness.adapters.hermes.TOOLSETS` to `EXP_TOOLSETS`, then replaces the
  runner's evaluate stage with a loop over the tests in `EXP_TESTS`, three at a time, each run and
  graded as `evaluate_parallel.py` does, app-call reconciliation included.
- **What it skips.** `verify_checkpoint`, because the frozen profiles were edited, and the
  comparison with the run's identity file, because the clone's adapter identity names the clone's
  own paths. That comparison would also have stopped the run when `hermes_past.py` changed at 10:29
  CDT, before the restart. It does not collect the phase cost, so the clone's `costs/tests.json`
  still holds the first pass's $11.888629; the published test cost is summed from the agent relay's
  per-turn log.
- **Resumes.** The log shows the first launch, a restart after Docker Desktop froze, and a last
  resume with a copy of the script that sets `EXP_TESTS="riley:158"`, to grade that test.
- **Leftovers.** The script's header comment comes from an earlier experiment script (ten document
  tasks, control against compact recall). It also starts `compact_proxy.py`, that experiment's
  recall-shortening proxy on port 65499, which this run did not use: its frozen profiles recall
  from past.dev on port 65407.
