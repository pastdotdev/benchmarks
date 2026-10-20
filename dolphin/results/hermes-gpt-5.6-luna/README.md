# Hermes + GPT 5.6 Luna + past.dev

This agent passed 490 of 600 tests. Its complete run cost $196.72, and its median task latency was 51.6 seconds.

Agent model: $176.61. Memory processing: $20.10.

| Persona | Passed | Test agent cost | Median latency | Runs |
| --- | ---: | ---: | ---: | --- |
| Morgan | 173 / 200 | $2.146275 | 43.334 s | [View](morgan/) |
| Alex | 148 / 200 | $2.664457 | 54.928 s | [View](alex/) |
| Riley | 169 / 200 | $3.043335 | 56.147 s | [View](riley/) |

- [Configuration](configuration.json)
- [Cost](cost.json)

## Notes

- Ingestion is the original run's: 13,539 Hermes turns with DolphinBench's public Hermes adapter toolsets (`hermes-cli`, `dolphinbench-apps`, `memory`, `session_search`), each completed turn stored in past.dev.
- The tests ran with the toolsets of the test-phase launch manifests of DolphinBench's official Built-in run with GPT 5.6 Luna (`dolphinbench-apps`, `memory`, `session_search`). Morgan's are the second test pass's. Alex's and Riley's ran a third time, after their 59 pushes without memories were processed again; in the second pass they passed 151 and 165. The first test pass, with `hermes-cli`, passed 363 of 600. The agent calls of the tests not reported are not in the totals above.
- The agent model and the judge ran through OpenRouter, pinned to its Azure provider with no fallback. Costs are what OpenRouter reported for every agent call, set-aside attempts included, plus past.dev credits at list price ($0.30 per 1,000). The table's test agent cost sums each test's final attempt; the $0.030507 of the 7 set-aside test attempts is in `cost.json`.
- [How this run was produced](../../README.md#how-the-hermes-result-was-produced), and [what it discloses](../../README.md#disclosures)