# Own agent + GPT 5.6 Luna + past.dev

This agent passed 520 of 600 tests. Its complete run cost $15.77, and its median task latency was 9.8 seconds.

Agent model: $7.51. Memory processing: $8.27.

| Persona | Passed | Test agent cost | Median latency | Runs |
| --- | ---: | ---: | ---: | --- |
| Morgan | 182 / 200 | $1.506875 | 8.878 s | [View](morgan/) |
| Alex | 168 / 200 | $1.701207 | 9.884 s | [View](alex/) |
| Riley | 170 / 200 | $1.830465 | 11.041 s | [View](riley/) |

- [Configuration](configuration.json)
- [Cost](cost.json)

## Notes

- The agent is past.dev's own minimal harness (`past_harness.py`): a tool-calling loop over OpenRouter with GPT 5.6 Luna at high reasoning, at most 60 turns. During ingestion it replies to each message with reasoning off and no tools; past.dev stores the user's messages and leaves the replies out.
- past.dev's recall for each request is placed before it (`memory_at_start` in the test records), and the agent can recall again with `past_recall`.
- These are the records of the ZIP submitted to DolphinBench (SHA-256 `ec59d4bfc153239c2142e1ea6f50ea2c36812b4d09a4cfb3518038244c9e3933`), which DolphinBench's validator accepts at 520 of 600. The judge ran through OpenRouter, pinned to its Azure provider; OpenRouter's extra judge message fields (`refusal`, `reasoning`, `reasoning_details`) were removed from 576 grade files before packaging.
- Costs are what OpenRouter reported for every agent call, plus the past.dev credits billed, at list price ($0.30 per 1,000).
- [How this run was produced](../../README.md#pastdevs-own-agent)
