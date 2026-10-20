# Hermes + GPT 5.6 Luna + past.dev

This agent passed 489 of 600 tests. Its complete run cost $196.56, and its median task latency was 46.3 seconds.

Agent model: $176.46. Memory processing: $20.10.

| Persona | Passed | Test agent cost | Median latency | Runs |
| --- | ---: | ---: | ---: | --- |
| Morgan | 173 / 200 | $2.146275 | 43.334 s | [View](morgan/) |
| Alex | 151 / 200 | $2.468901 | 47.747 s | [View](alex/) |
| Riley | 165 / 200 | $2.772112 | 48.039 s | [View](riley/) |

- [Configuration](configuration.json)
- [Cost](cost.json)

## Notes

- Ingestion is the original run's: 13,539 Hermes turns with DolphinBench's public Hermes adapter toolsets (`hermes-cli`, `dolphinbench-apps`, `memory`, `session_search`), each completed turn stored in past.dev.
- The 600 tests ran again on that same frozen memory with the toolsets of the test-phase launch manifests of DolphinBench's official Built-in run with GPT 5.6 Luna (`dolphinbench-apps`, `memory`, `session_search`). The first test pass, with `hermes-cli`, passed 363 of 600; its $11.89 of agent calls is not in the totals above.
- The agent model and the judge ran through OpenRouter, pinned to its Azure provider with no fallback. Costs are what OpenRouter reported for every agent call, set-aside attempts included, plus past.dev credits at list price ($0.30 per 1,000). The table's test agent cost sums each test's final attempt; the $0.338209 of the 9 set-aside test attempts is in `cost.json`.
- [How this run was produced](../../README.md#how-the-hermes-result-was-produced), and [what it discloses](../../README.md#disclosures)
