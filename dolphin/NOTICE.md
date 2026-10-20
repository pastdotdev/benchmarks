# Notices

## DolphinBench

This folder runs [DolphinBench](https://github.com/mem0ai/dolphinbench) by Mem0, licensed under
the Apache License 2.0, at commit `81cb6f8`.

- **Code.** The harness imports DolphinBench's runner, adapters, app connection, record types and
  grader at run time. `hermes_past.py` extends DolphinBench's Hermes adapter
  (`harness/adapters/hermes.py`), and its `_attempt` method reuses that adapter's code with
  changes. `evaluate_parallel.py`, `ingest_parallel.py` and `scripts/run_subset.py` adapt the
  stage loops of DolphinBench's runner (`harness/runner.py`). The `IDENTITY` paragraph in
  `past_harness.py` is adapted from DolphinBench's `harness/benchmark_soul.md`, with the agent's
  name removed. These adapted portions remain under the Apache License 2.0; the rest of this
  folder is under this repository's MIT license.
- **Records.** The files in `results/` contain DolphinBench content: its personas' history
  messages, its test requests, its simulated app data and its grading requirements, alongside
  model-generated answers, tool calls and judge verdicts. The DolphinBench-derived content keeps
  its Apache License 2.0 terms and is excluded from this repository's MIT license. The records
  follow the layout of DolphinBench's official run records and were written with functions of its
  `website/scripts/build_public_run_records.py`.

Paper: Soumil Rathi, Deshraj Yadav and Taranjeet Singh, "DolphinBench: Mapping the Pareto
Frontier of Agent Memory", [arXiv:2609.24971](https://arxiv.org/abs/2609.24971), 2026.

## Hermes Agent

The Hermes run used [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research,
under the MIT License, with DolphinBench's evaluation patch and past.dev's memory plugin. No Hermes
code is included here.
