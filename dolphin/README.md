# DolphinBench on past.dev

[DolphinBench](https://github.com/mem0ai/dolphinbench) is Mem0's benchmark of agent memory. It has
three simulated users, each with about 500,000 tokens of dated history, and 600 tests (200 per
user) in which an agent acts for the user through simulated apps such as email, calendar, chat,
documents and code review. A test depends on something the user said earlier, and it is graded on
what the agent does: deterministic checks on its app calls and their arguments, and a model judge
for written content. A configuration first sends each user's whole history through its agent and
memory, freezes the memory, and then runs that user's tests in fresh conversations.

## Results

Hermes Agent with GPT 5.6 Luna and past.dev as its memory passed **490 of 600** tests (81.7%,
Wilson 95% interval 78.4% to 84.6%). In DolphinBench's official runs of the same agent and model,
the other memories pass 355 to 424. past.dev ran self-hosted on the run's machine for these runs,
not on the cloud API.

The memory tasks, the 339 tests that expect an app action other than a document, carry the result:
past.dev passed 280 of them, against 196 to 235 for the official rows.

| Memory | Passed | Morgan | Alex | Riley | Cost | Median latency | Files |
|---|---:|---:|---:|---:|---:|---:|---|
| **past.dev** | **490 / 600** | 173 | 148 | 169 | $196.72 | 51.6 s | [Run](results/hermes-gpt-5.6-luna/) |
| Mem0 | 424 / 600 | 156 | 132 | 136 | $96.21 | 37.7 s | [Official](https://github.com/mem0ai/dolphinbench/tree/81cb6f8405b40a9e76089cef650806a80af06ea2/results/hermes/gpt-5.6-luna/mem0) |
| Hindsight | 417 / 600 | 144 | 127 | 146 | $84.65 | 55.3 s | [Official](https://github.com/mem0ai/dolphinbench/tree/81cb6f8405b40a9e76089cef650806a80af06ea2/results/hermes/gpt-5.6-luna/hindsight) |
| Honcho | 411 / 600 | 144 | 120 | 147 | $142.85 | 44.7 s | [Official](https://github.com/mem0ai/dolphinbench/tree/81cb6f8405b40a9e76089cef650806a80af06ea2/results/hermes/gpt-5.6-luna/honcho) |
| Built-in | 394 / 600 | 131 | 111 | 152 | $61.48 | 44.4 s | [Official](https://github.com/mem0ai/dolphinbench/tree/81cb6f8405b40a9e76089cef650806a80af06ea2/results/hermes/gpt-5.6-luna/builtin) |
| Supermemory | 355 / 600 | 138 | 98 | 119 | $345.64 | 52.7 s | [Official](https://github.com/mem0ai/dolphinbench/tree/81cb6f8405b40a9e76089cef650806a80af06ea2/results/hermes/gpt-5.6-luna/supermemory) |

Each persona has 200 tests. Cost is the agent model plus memory processing, with grading left out,
as in DolphinBench's `cost.json`. Latency is the median test's duration. The official rows are
DolphinBench's own runs at commit `81cb6f8`; the past.dev row is our run of the same agent, outside
DolphinBench's official results. [Disclosures](#disclosures) lists every way it differs from them,
including an ingestion whose agent calls cost 3.2 times the official Built-in run's.

Sources: for past.dev, `passed` and `latency_seconds` in
`results/hermes-gpt-5.6-luna/<persona>/tests.json` and `results/hermes-gpt-5.6-luna/cost.json` in
this folder, built from the run's grade files and the cost logs under [Costs](#costs). For the official rows, the same fields in DolphinBench's
`results/hermes/gpt-5.6-luna/<memory>/<persona>/tests.json` and `cost.json`.

Matched test by test, past.dev passes more of the tests where it and an official row disagree: 115
to 49 against Mem0, 119 to 46 against Hindsight, 112 to 33 against Honcho, 133 to 37 against
Built-in and 165 to 30 against Supermemory (exact McNemar p below 2.8e-7 for each). Each side is
a single run, and single runs vary: when Alex's and Riley's 400 tests ran again after their pushes
without memories were processed, 69 changed outcome, 35 to a pass and 34 to a fail.

### Memory and document tasks

A document task asks the agent to write a document: its `expected_tool_calls` in DolphinBench's
`tests/<persona>/<id>.yaml` include `create_doc`. There are 261, most of them Riley's. The other 339
tests, called memory tasks here, expect other app actions, such as an email, a calendar event or an
order.

| | Memory tasks | Morgan | Alex | Riley | Document tasks | Morgan | Alex | Riley |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Tests | 339 | 149 | 151 | 39 | 261 | 51 | 49 | 161 |
| **past.dev** | **280** | 130 | 115 | 35 | **210** | 43 | 33 | 134 |
| Mem0 | 235 | 115 | 97 | 23 | 189 | 41 | 35 | 113 |
| Hindsight | 234 | 106 | 98 | 30 | 183 | 38 | 29 | 116 |
| Honcho | 227 | 110 | 90 | 27 | 184 | 34 | 30 | 120 |
| Built-in | 202 | 92 | 81 | 29 | 192 | 39 | 30 | 123 |
| Supermemory | 196 | 102 | 71 | 23 | 159 | 36 | 27 | 96 |

past.dev passes 280 of 339 memory tasks, against 196 to 235 for the official rows, and 210 of 261
document tasks, against 159 to 192.

Sources: the `tests.json` files above, split by `expected_tool_calls` in DolphinBench's
`tests/<persona>/<id>.yaml` at commit `81cb6f8`.

### past.dev's own agent

past.dev's own minimal agent with the same model passed **520 of 600** (86.7%, Wilson 95%
interval 83.7% to 89.2%) for $15.77, with a median test latency of 9.8 s. It is a different agent
from Hermes, so it is reported as a separate configuration.

| Configuration | Passed | Morgan | Alex | Riley | Memory tasks | Document tasks | Cost | Median latency | Files |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Own agent + GPT 5.6 Luna + past.dev | **520 / 600** | 182 | 168 | 170 | 293 / 339 | 227 / 261 | $15.77 | 9.8 s | [Run](results/own-agent-gpt-5.6-luna/) |

- **Agent.** `past_harness.py` is a tool-calling loop over OpenRouter's chat completions
  (`model.py`): `openai/gpt-5.6-luna` at high reasoning, at most 60 turns, on OpenRouter's default
  provider routing. Its prompt is DolphinBench's assistant identity (`harness/benchmark_soul.md`)
  without the agent's name, followed by a paragraph about past.dev.
- **Memory.** During ingestion the model replies to each history message with reasoning off and no
  tools. past.dev stores the user's message with its date, local time and author, 100 to a push,
  and the replies stay out of memory. At the freeze the projects held 17,448 memories for Morgan,
  19,299 for Alex and 21,595 for Riley.
- **Tests.** past.dev's recall for the request comes before it as a system message
  (`memory_at_start` in the records), and the agent can recall again with `past_recall`.
- **past.dev.** The cloud API.
- **Harness version.** Ingestion and Morgan's tests 001 to 099 ran `past_harness.py` 1.0. From
  Morgan's test 100 on, version 1.1 keeps a recall query's first 1,000 characters, the most
  past.dev accepts. This folder's file is 1.2, the
  version the Hermes run used: it adds list-price credits for a deployment that bills nothing, a
  JSON-lines reader that splits on newlines only, and a freeze that stops on a push past.dev kept
  no memory from (this folder's `past_client.py` adds the `explain` call that check uses). The own
  agent's freeze therefore did not check whether every push gave memories, and that cannot be
  checked now without calling past.dev.
- **Judge.** As for the Hermes run, through `judge_relay.py`, pinned to OpenRouter's Azure
  provider. OpenRouter's extra judge message fields (`refusal` and `reasoning` in 602 judge
  messages, `reasoning_details` in 153) were removed from 576 of the 600 grade files before
  packaging, with nothing else in them changed.
- **Submission.** The records are those of the run's `submission.zip`, which DolphinBench's
  validator code accepts at 520 / 600 and $15.774289 (SHA-256 `ec59d4bf…`). We submitted it to
  DolphinBench's site.
- **Cost.** $15.774289: ingestion $10.735742 (model $2.468942 plus 27,556 past.dev credits,
  $8.2668) and tests $5.038547.

Sources: `results/own-agent-gpt-5.6-luna/` in this folder.

## How the Hermes result was produced

The result pairs the original run's ingestion with later test passes. The original run ingested
every history message and ran the tests once; the tests then ran again on the same memory, with
the three toolsets DolphinBench's official Built-in run gives Hermes in its test phase. After that,
Alex's and Riley's pushes that had given no memories were processed again, and their 400 tests ran
a third time. The reported result is Morgan's tests from the second pass and Alex's and Riley's
from the third.

### Setup

- **Agent.** Hermes Agent at `NousResearch/hermes-agent` commit `935c1e8`, with DolphinBench's
  evaluation patch (`examples/reference/hermes-evaluation.patch`, SHA-256 `24e3d352…`) and
  past.dev's memory plugin in `plugins/memory/past` (`__init__.py` SHA-256 `422df786…`; the copy in
  [`hermes-plugin/past/`](hermes-plugin/past) differs from it by one comment edited for publication). It ran in agent image
  `sha256:bd8aafd99ba172e1842b39bbc79de23d0d61b3cafa6ea1a02e9423df74cdba78`, built with
  DolphinBench's image builder, with the two packages Hermes otherwise installs on first use
  (`boto3` and `edge-tts`) added. The profile is DolphinBench's Hermes + Luna reference profile with
  `memory.provider: past`.
- **Model.** GPT 5.6 Luna at high reasoning, at most 60 turns, through `agent_relay.py`.
- **Memory.** Each completed Hermes turn becomes two past.dev data points, the user's message and
  Hermes' reply, with the message's date, local time and author, 100 to a push. The agent recalls
  through the plugin's `past_recall` tool: one query per call, up to 100 results within 8,000
  tokens. past.dev ran self-hosted on the same machine.
- **Judge.** GPT 5.6 Sol at medium reasoning with DolphinBench's fixed settings (an 8,000-token
  limit and JSON responses), through `judge_relay.py`.

### Original run

The original run (`hermes-past-full`) started on 2026-10-02 at 16:14 CDT. Its ingestion sent all
13,539 history messages through Hermes, one turn each, the three personas in parallel
(`ingest_parallel.py`), with the toolsets of DolphinBench's public Hermes adapter
(`harness.adapters.hermes.TOOLSETS`: `hermes-cli`, `dolphinbench-apps`, `memory`,
`session_search`). Hermes sent 72,448 model calls to `agent_relay.py`, and the ingestion finished
on 2026-10-05 at 20:18 CDT. At the freeze the projects held 28,589 memories for Morgan, 31,027 for
Alex and 38,818 for Riley.

Its first test pass used the same toolsets, finished on 2026-10-06 at 00:22 CDT and passed 363 of
600 (Morgan 133, Alex 132, Riley 98): 274 of 339 memory tasks and 89 of 261 document tasks.
`hermes-cli` gives Hermes a terminal, file tools and code execution, and on document tasks the agent
often saved the document as a local file. 159 of the 261 document tasks did not call `create_doc`,
and 96 of those used `write_file`, `patch`, `terminal` or `execute_code`.

### Rerun with the official toolsets

DolphinBench's own reference code gives Hermes three toolsets: `MATRIX_TOOLSETS =
["dolphinbench-apps", "memory", "session_search"]` (`reference/artifacts.py`, line 44, at commit
`81cb6f8`), which the official memory rows extend with their own (`[*MATRIX_TOOLSETS, "hindsight"]`
and so on). The launch manifests of its official Hermes + GPT 5.6 Luna Built-in run
(`builtin-configuration.zip`, listed in DolphinBench's `website/content/official-evidence.json`)
give the agent the same three in the test phase. The public adapter adds `hermes-cli`, so the first
pass did not match the official setup. The 600 tests ran again with those three, on an APFS copy of the finished run
(`exp-toolsets-600`). Each persona's frozen Hermes profile had its `toolsets` and
`platform_toolsets` set to the three, and `scripts/run_subset.py`, launched by
`scripts/run_toolsets_600.sh`, set DolphinBench's `harness.adapters.hermes.TOOLSETS` to the same
three and ran the tests three at a time. The rerun recalled from the original run's past.dev
projects, and its ingestion records, checkpoints and push receipts are byte-identical copies of the
original run's.

In every rerun test the model was offered `past_recall` and Hermes' tool search, through which it
reached the app tools and `session_search`. No `hermes-cli` tool appears in any rerun test's tool
list or calls, and 251 of the 261 document tasks called `create_doc`. The rerun ran on 2026-10-06
from 09:45 to 20:04 CDT, with a stop from 10:31 to 17:49 while Docker Desktop was frozen, and passed
489 of 600. Against the first pass, 128 document tasks went from fail to pass and 6 from pass to
fail; memory tasks moved both ways, 31 to a pass and 27 to a fail.

Ingestion kept the public adapter's toolsets. The memory behind the result is the original run's,
built by Hermes with `hermes-cli`: 5,267 of its 13,539 turns (38.9%) called a `hermes-cli` tool.

### Alex and Riley after their pushes were processed again

At the freeze, 59 of Alex's and Riley's pushes had given no memories (see [Memory](#memory)). After
the second pass, those pushes were processed again on the same self-hosted deployment, and Alex's
and Riley's 400 tests ran again on a copy of the second pass, with the same frozen profiles,
toolsets, adapter and judge (`scripts/run_reextract_ar.sh`), on 2026-10-19 from 15:32 to 17:52
CDT. Alex passed 148 (151 in the second pass) and Riley 169 (165). With Morgan's 173 from the second
pass, the result is 490 of 600. Behind the net +1, 35 tests went from fail to pass and 34 from pass
to fail.

## Disclosures

Each item below is a way this run differs from DolphinBench's official Hermes + GPT 5.6 Luna runs
or their procedure, with its count.

### Agent and tools

- **Ingestion toolsets.** Ingestion used the public adapter's four toolsets, `hermes-cli`
  included. 5,267 of 13,539 turns (38.9%) called a `hermes-cli` tool, most often `skill_view` (4,310
  turns), `execute_code` (1,057), `terminal` (873), `clarify` (500), `web_extract` (283),
  `search_files` (252) and `web_search` (238). The ingestion's agent calls cost $168.73, 3.2 times
  the official Built-in run's $53.32 (the ingestion totals in its `cost-calculation.json`, inside
  `builtin-configuration.zip`). The official manifests at hand cover the test phase only, so the
  toolsets the official runs ingested with are unknown here.
- **Web access during ingestion.** With `hermes-cli`, Hermes could search and read the web while it
  ingested. Every `web_search` and `web_extract` call in the ingestion traces,
  1,336 calls with their results, was scanned for `dolphin`, `mem0`, `github`, `huggingface` and
  `benchmark`. One reached DolphinBench: in Alex's ingestion turn dated 2024-05-08 (trace
  `20261003_123218_4fd609`), a search for "Park Slope quiet cafe 7th Avenue cortado crossword"
  returned dolphinbench.ai's page for Alex's test 072, with the test's request and grading criteria.
  The search terms came from the turn's earlier `past_recall`, which had already led Hermes to 7th
  Avenue, and its reply, which went into past.dev, names a café on 7th Avenue and cortados and
  nothing else from the page. Test 072 passed in every test pass; without it the reported result
  is 489 of 600. The other matches were documentation pages on GitHub and pages using "benchmark"
  in another sense.
- **Several test passes.** The reported tests come from later passes on the same ingestion: Morgan's
  from the second and Alex's and Riley's from a third, after their pushes without memories were
  processed again. The first pass, with `hermes-cli`, passed 363 of 600, and the second 489.
- **Setup chosen on the same tests.** Before the second pass, side experiments ran on the same frozen
  memory. Eight variants ran 10 document tasks that had all failed in the first pass: the official
  three toolsets passed 8 of the 10, and the seven others passed 0 to 4. A variant with a 37-byte
  `SOUL.md`, still with `hermes-cli`, ran 62 of Morgan's tests, passed 44 and was stopped. The
  official test toolsets were chosen after these, with no held-out tests. The reported tests pass 8 of
  those 10 document tasks and 56 of those 62 Morgan tests. A ninth variant on the 10 document tasks,
  started during the second pass's stop, crashed before grading.
- **Adapter version.** `hermes_past.py` in this folder is the version both runs' `run.json` record
  (SHA-256 `05d028e4…`). It ran the ingestion, the first pass and the second pass's first 160 tests,
  all Morgan's. The second pass's other 440 tests and the Alex and Riley rerun ran with a later edit
  of it (`0bd3efeb…`), given as
  [`scripts/hermes_past-segment2.diff`](scripts/hermes_past-segment2.diff): it adds a
  `BENCH_TOOLSETS` constant with the three test toolsets, gives it to new profiles and to every
  turn, and reads the recall settings from the plugin. The model saw the same tool lists in both
  segments, and Morgan's system prompts differ only in the profile name. Both later passes skipped the
  comparison with the run's identity file (see [Memory](#memory)), which would otherwise have
  stopped it on the changed file.
- **Model route.** Hermes kept the reference profile's `azure-foundry` provider, with its base URL
  set to `agent_relay.py`, which sent every call to OpenRouter, pinned to its Azure provider with no
  fallback. The official runs called an Azure Foundry endpoint directly (the base URL in the
  Built-in run's launch manifests). Model and settings are the same: GPT 5.6 Luna, high reasoning,
  at most 60 turns. The relay also ends a turn whose answer came back empty (see Replyless turns).
- **Judge route.** The judge's calls went through OpenRouter, pinned to Azure (`judge_relay.py`),
  with DolphinBench's fixed model and settings. The relay logs report Azure for all 593 judge calls
  of the second pass, all 407 of the Alex and Riley rerun and all 437 of the first pass. The official runs call an Azure deployment directly.

### Memory

- **Self-hosted past.dev.** The Hermes runs used past.dev self-hosted on the same machine; the own
  agent used the cloud API. 272 pushes carried 27,049 data points: 13,539 messages and 13,510
  replies. A self-hosted deployment bills nothing, so memory is priced at past.dev's cloud list
  price: one credit per 350 bytes of each data point, at least one, 67,003 credits at $0.30 per
  1,000, $20.10. Recall is included, as on the Flex plan.
- **Pushes without memories.** At the freeze, for 30 of Alex's 101 pushes (72 to 101) and 29 of Riley's 103 (75
  to 103), past.dev's explain call (`GET /api/v1/ingest/{id}/explain`) reported no memories; their
  data points were stored and searchable. These pushes cover the latest part of each history: 1,459
  of Alex's 5,011 messages (29.1%, ids 3553 to 5011) and 1,426 of Riley's 5,128 (27.8%, ids 3703 to
  5128). The run froze these two projects with `turn_recovery.freeze_disclosing`, which lists those
  pushes in the checkpoint. All 68 of Morgan's pushes gave memories. After the second pass, the 59
  pushes were processed again on the same deployment, so Alex's and Riley's reported tests ran on
  that memory, not on the memory frozen at the end of ingestion (see
  [Alex and Riley after their pushes were processed again](#alex-and-riley-after-their-pushes-were-processed-again)).
- **Frozen memory not rechecked.** The freeze recorded 28,589 memories for Morgan, 31,027 for Alex
  and 38,818 for Riley. The later passes skipped the adapter's `verify_checkpoint`, which compares the
  frozen profiles' hashes and each project's memory count and request log with the checkpoint,
  because the profiles had been edited. They also skipped the comparison with the run's identity
  file, because the copy's identity names the copy's own paths. A later count showed
  unchanged counts for Morgan, whose reported tests are the second pass's, and 38,837 memories for
  Riley, 19 more than at the freeze.

### Records and grading

- **App-call reconciliation.** DolphinBench's mock app server logs each call with its own defaults
  filled in and ignores arguments it does not know, so its log can differ from the call the agent
  sent, and DolphinBench's checks then find no matching call in the conversation. Hermes also drops
  a duplicated parallel read-only call from its conversation after the server ran it. App calls were
  reconciled to what the agent sent: in ingestion, 1,570 records (Morgan 178, Alex 769, Riley 623),
  with 2,089 calls set to the agent's arguments and 5 read-only calls removed; in the first pass,
  256 entries in 195 tests; in the reported tests, 225 calls in 177 of 600 tests (Morgan 82,
  Alex 72, Riley 23), 145 of which passed. The pairing is imperfect: in Alex's tests 006, 022, 094 and 157, a logged `list_open_prs` call with status
  `closed` was paired with an agent call that sent no status, and no check grades that tool. None
  of the 225 calls carries an argument the server ignored: 198 differ only by fields the
  server added with its own defaults, such as `"cc": null`, and 27 are `list_open_prs` calls whose
  `status` the agent sent as null and the server logged as `open`.
- **Attempts run again.** Attempts that could not be recorded were set aside, their cost counted,
  and run again. Ingestion: 78 attempts on 70 of 13,539
  turns (24 recording failures, 13 rewritten conversations, 13 that returned a different user
  message or conversation boundary, 11 interruptions, 10 content-filter stops on 9 turns, 4 full
  disks and 3 Hermes timeouts). First pass: 11 attempts on 11 tests, 4 of them set aside by hand
  after grading could not run. Reported tests: 7 attempts on 6 tests (3 Docker failures when
  another run's setup removed the agent containers and 3 interrupted when Docker Desktop froze, in
  Morgan's tests 159 to 163, and a recording failure in Riley's test 121). All 6 passed when run
  again; counting them as failures gives 484 of 600. Each of the 600 final test records comes from a single Hermes
  attempt.
- **Continuation prompt.** 14 set-aside ingestion attempts (Morgan 10, Riley 4) end at Hermes'
  own continuation prompt (`[System: Continue now.`). All ran again, and no final ingestion record
  or test trace contains the prompt, so no turn was cut at it.
- **Replyless turns.** In 29 ingestion turns (Morgan 24, Alex 2, Riley 3) the model's answer after
  tool calls came back empty. Hermes takes an empty answer for a broken response and sends the same
  request again, up to eight times; `agent_relay.py` answers such a repeat with a 400
  `empty_completion` error without calling the model (29 of the 72,448 ingestion calls), so the
  turn ends there. The 29 turns were recorded as complete and sent to past.dev with the user's
  message and no reply. No test turn ended this way.
- **Judge message fields.** OpenRouter adds `refusal`, `reasoning` and `reasoning_details` to the
  judge's message, and DolphinBench's packager refuses them. They were removed from the first
  pass's 408 judged grade files. 557 of the
  reported tests' 600 grade files still carry them: `refusal` and `reasoning` in 586 judge messages,
  `reasoning_details` in 175. The records in this folder keep verdicts and checks only, and the
  replay in [Scores](#scores) drops the fields in memory.
- **No submission ZIP.** The Hermes ingestion rows come to 1,341.5 MiB of JSON (1,059.4 MiB without
  system messages), over DolphinBench's 512 MiB limit per file, so the run's package stage stopped
  and the result cannot be uploaded to DolphinBench's site. Its grades were checked with the replay
  under [Scores](#scores).

### Cost and latency

| | Ingestion | Tests | Total |
|---|---:|---:|---:|
| Agent model, as OpenRouter reported | $168.73 | $7.88 | $176.61 |
| past.dev, 67,003 credits at list price | $20.10 | $0.00 | $20.10 |
| Total | $188.83 | $7.88 | $196.72 |

Sources: the relay logs and push receipts listed under [Costs](#costs).

- **Cost method.** Agent cost is what OpenRouter reported for every call Hermes sent through the
  relay, set-aside attempts included. The official `cost.json` totals price recorded tokens at
  DolphinBench's saved rates. 1,206 of 72,448 ingestion calls, 120 of 5,180 first-pass calls and
  14 of the reported tests' 3,992 calls came back without a cost and count as zero; at the mean
  priced call they would add about $2.86, $0.28 and $0.03. One ingestion call never ended.
- **Test phase.** The reported tests' $7.88 of agent calls compares with $3.23 (Mem0) to $8.05 (Built-in)
  in the official runs, summed from their per-test `cost_usd`. Most of the cost gap is ingestion.
- **Left out.** Grading, as in DolphinBench's cost files: $2.60 for the first pass's 437 judge
  calls, $4.09 for the second pass's 593 and $3.17 for the Alex and Riley rerun's 407. The agent
  calls of the tests not reported: $11.89 in the first pass and $5.56 for Alex's and Riley's tests in
  the second. Side experiments and aborted runs.
- **Latency.** Latency is DolphinBench's per-test duration on one Mac running three tests at a time
  under Docker Desktop: median 51.6 s and p95 78.3 s in the reported tests, median 58.5 s and p95
  103.3 s in the first pass.

## Files

### Records

`results/` has one folder per configuration, in the layout of DolphinBench's official run records
(`results/<harness>/<model>/<memory>/` in its repository):

| Path | Contents |
|---|---|
| `results/hermes-gpt-5.6-luna/` | The reported Hermes result: Morgan's tests from the second pass and Alex's and Riley's from the third, on the original run's ingestion |
| `results/own-agent-gpt-5.6-luna/` | The own agent's result, from its submission ZIP |
| `<run>/README.md` | Score, cost and latency, by persona |
| `<run>/configuration.json` | Harness, model, memory and runtime settings |
| `<run>/cost.json` | Agent and memory cost, by persona |
| `<run>/<persona>/README.md` | Each test's result, latency and agent call cost |
| `<run>/<persona>/ingestion.json` | The agent's reply to each history message |
| `<run>/<persona>/tests.json` | Each test's request, memory at the start, memory calls, app calls and results, answer, grading checks, latency and cost |

They were written with DolphinBench's `website/scripts/build_public_run_records.py`, with
`past_recall` added to the memory tools it recognizes.

### Code

The files stand as the second pass and the Alex and Riley rerun ran them, except `hermes_past.py`, which is the version
the runs' identity files record ([Disclosures](#agent-and-tools) gives the later edit), and [past.dev's own agent](#pastdevs-own-agent) where
that run used `past_harness.py` 1.0 and 1.1. `turn_recovery.py`, `ingest_parallel.py` and
`evaluate_parallel.py` are not in the runs' identity files, and they changed while the original run
was being resumed: `ingest_parallel.py` last at 19:16 CDT on 2026-10-05, during the ingestion,
`evaluate_parallel.py` at 20:28 CDT that day, during the first pass, and `turn_recovery.py` at 00:27
CDT on 2026-10-06, after it. Earlier parts of the original run ran earlier versions, which this
folder does not include; both later passes ran the `turn_recovery.py` here. Comments in `past_harness.py`,
`turn_recovery.py`, `ingest_parallel.py` and `hermes-plugin/past/__init__.py` were edited for
publication; their code is unchanged.

| File | What it does |
|---|---|
| `hermes_past.py` | The Hermes + past.dev adapter: DolphinBench's Hermes adapter with past.dev's plugin, pushes, freeze, identity and cost |
| `past_harness.py` | The own agent, as the runner's five methods; also past.dev's projects and pushes for `hermes_past.py` |
| `past_client.py` | Every past.dev API call |
| `model.py` | The own agent's model calls through OpenRouter |
| `agent_relay.py` | Sends Hermes' model calls to OpenRouter's Azure provider and logs each call's cost |
| `judge_relay.py` | Sends DolphinBench's judge calls to OpenRouter's Azure provider and logs each call's cost |
| `ingest_parallel.py` | The ingest stage with the three personas at once (Hermes adapter only) |
| `evaluate_parallel.py` | The evaluate stage with several tests at once |
| `turn_recovery.py` | Runs again the turns Hermes could not complete, reconciles app calls, and freezes with the pushes without memories listed |
| `requirements.txt` | The Python dependency beyond DolphinBench's own |
| `hermes-plugin/past/` | past.dev's Hermes memory plugin as the runs used it: the `past_recall` tool and the turn spool |
| `scripts/` | The run scripts and run files, with this machine's paths replaced by placeholders, and `hermes_past-segment2.diff` ([scripts/README.md](scripts/README.md)) |
| `NOTICE.md` | Credits for DolphinBench and Hermes Agent |

## Check the results

The run's raw logs, traces and grade files are not in this repository. The records in `results/`
carry each test's grading checks and verdict, which the check below reads.

### Scores

A test passes when its grade has at least one check and every check passed, as in DolphinBench's
`graders/mechanical.py`. From this folder:

```bash
python3 - results/hermes-gpt-5.6-luna <<'EOF'
import json, sys
from pathlib import Path
run, total = Path(sys.argv[1]), 0
for persona in ("morgan", "alex", "riley"):
    tests = json.loads((run / persona / "tests.json").read_text())["tests"]
    checks = [test["grading"]["checks"] for test in tests]
    passed = sum(1 for c in checks if c and all(check["passed"] for check in c))
    print(persona, passed, "of", len(tests))
    total += passed
print("total", total)
EOF
```

It prints 173, 148 and 169, 490 in all, and with `results/own-agent-gpt-5.6-luna` 182, 168 and 170,
520 in all. For the memory and document split, a test is a document task when its
`expected_tool_calls` in DolphinBench's `tests/<persona>/<id>.yaml` include `create_doc`.

We also replayed every reported grade through DolphinBench's grader
(`graders.mechanical.grade_tool_trace`) with its recorded judge answers
(`graders.judge_recording.replay_judge`), the check DolphinBench's validator applies to a
submission, with the relay-only fields dropped in memory. All 600 give their saved verdicts, 490
passes with no error and no mismatch. The `tests.json` files in this folder hold the same verdicts.

### Costs

- **Agent.** The sum of `cost_usd` over the `end` events in `agent-relay.jsonl`, by tag prefix:
  `hermes-past-full.ingestion.` gives $168.729973 over 72,448 calls, and
  `exp-toolsets-600.tests.morgan.`, `exp-reextract-ar.tests.alex.` and `exp-reextract-ar.tests.riley.`
  give $7.884574 over 3,992 calls.
- **Memory.** The sum of `listCredits` in `past/receipts/<persona>.json`: 67,003 credits (16,441,
  24,135 and 26,427 for Morgan, Alex and Riley).
- **Judge.** The sum of `cost_usd` in `judge-relay.jsonl`: $2.600145 over 437 calls for the first
  pass, $4.093833 over 593 for the second and $3.166711 over 407 for the Alex and Riley rerun.

## Run it

Both configurations run from a DolphinBench checkout at commit `81cb6f8`, with this folder on
`PYTHONPATH`. The commands below assume this repository is cloned next to it as `benchmarks`.

### Own agent

You need a past.dev organization key and an OpenRouter key. The harness creates one project per
persona, three per run, and never deletes them; an account whose project limit is lower cannot run
all three personas.

```bash
git clone https://github.com/mem0ai/dolphinbench.git && cd dolphinbench
git checkout 81cb6f8
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r ../benchmarks/dolphin/requirements.txt
cp ../benchmarks/dolphin/scripts/past-full.yaml .
export PYTHONPATH=../benchmarks/dolphin
export PAST_API_URL=... PAST_MANAGEMENT_KEY=... OPENROUTER_API_KEY=...
export AZURE_OPENAI_API_KEY=... AZURE_OPENAI_ENDPOINT=...
python ../benchmarks/dolphin/judge_relay.py &

python -m harness.runner prepare --config past-full.yaml       # checks the setup; no paid call
python -m harness.runner ingest --config past-full.yaml --confirm-paid-calls
python -m harness.runner evaluate --config past-full.yaml --confirm-paid-calls
python -m harness.runner package --config past-full.yaml       # tmp/past-dev-full/submission.zip
```

| Variable | What it holds |
|---|---|
| `PAST_API_URL` | past.dev's API address, `https://api.past.dev` for the cloud |
| `PAST_MANAGEMENT_KEY` | A past.dev organization key (Settings › Organization key) |
| `OPENROUTER_API_KEY` | An OpenRouter key, for the agent and the judge relay |
| `AZURE_OPENAI_API_KEY` | A local password you choose, which DolphinBench's runner sends to `judge_relay.py` in place of an Azure key |
| `AZURE_OPENAI_ENDPOINT` | `judge_relay.py`'s address (it listens on 127.0.0.1, port 8765 unless `JUDGE_RELAY_PORT` is set) |

The harness creates one project per persona for each run, named `dolphinbench-<persona>-<run tag>`,
and does not delete them. Run the same command again to resume; the runner never repeats a
completed interaction. A changed model, option or code file needs a new `output` folder.

### Hermes

The Hermes configuration also needs Docker, a Hermes checkout with past.dev's plugin, and a
past.dev deployment the agent's container can reach.

- **Hermes.** `NousResearch/hermes-agent` at `935c1e8`, DolphinBench's
  `examples/reference/hermes-evaluation.patch`, and past.dev's plugin in `plugins/memory/past`. Copy
  [`hermes-plugin/past/`](hermes-plugin/past) to `plugins/memory/past`: `plugin.yaml` matches the run's
  copy (`4d00b7e8…`), and `__init__.py` differs from the run's copy (`422df786…`) only by one comment
  edited for publication.
- **Image.** `python hermes_past.py build-image --source <hermes checkout> --memory builtin` builds
  DolphinBench's agent image, adds the two packages Hermes otherwise installs on first use, and
  prints the image id for `DOLPHINBENCH_AGENT_IMAGE`.
- **Variables.** Beyond the own agent's: `PAST_AGENT_API_URL` (past.dev as the agent's container
  reaches it), `AZURE_FOUNDRY_API_KEY` (a local password between Hermes and `agent_relay.py`),
  `AZURE_OPENAI_BASE_URL` (`agent_relay.py` as the container reaches it),
  `DOLPHINBENCH_HERMES_SOURCE` (the Hermes checkout), `DOLPHINBENCH_AGENT_IMAGE`, and
  `DOLPHINBENCH_DOCKER_CONTEXT` (the Docker context, needed on Docker Desktop for macOS).
  `AGENT_RELAY_LOG`, `AGENT_RELAY_TAGS` and `JUDGE_RELAY_LOG` set where the relays write their cost
  logs. The scripts also set `HERMES_MEMORY_SYNC_DRAIN_TIMEOUT_SECONDS` and `BUILDX_CONFIG`.
- **Original run.** `scripts/full_run.sh` starts both relays, then runs prepare, ingest
  (`ingest_parallel.py`), evaluate (`evaluate_parallel.py`, three workers) and package with
  `scripts/hermes-past-full.yaml`, and resumes when run again. Its package stage stops on
  DolphinBench's size limit.
- **Rerun.** `scripts/run_toolsets_600.sh`, with `EXP_ARMS=toolsets-600` and
  `EXP_TOOLSETS=dolphinbench-apps,memory,session_search` set, runs `scripts/run_subset.py` with
  `scripts/exp-toolsets-600.yaml` on a copy of the finished run whose frozen profiles have the three
  toolsets. `run_subset.py` runs the tests listed in `EXP_TESTS`, which the script sets to all 600.
- **Alex and Riley again.** `scripts/run_reextract_ar.sh`, with `EXP_ARMS=reextract-ar` set, runs
  `scripts/run_subset.py` with `scripts/exp-reextract-ar.yaml` on a copy of the second pass, for
  Alex's and Riley's 400 tests.
- **Toolsets.** This `hermes_past.py` uses DolphinBench's `harness.adapters.hermes.TOOLSETS`, which
  include hermes-cli, as the ingestion and the first pass did. The rerun's official toolsets come
  from `EXP_TOOLSETS` in `run_subset.py` and from the copy's edited profiles;
  `scripts/hermes_past-segment2.diff` makes them the adapter's own default.

The scripts use placeholders for this machine's paths and read the keys from four env files.
[scripts/README.md](scripts/README.md) lists each file's variables by name and how the copy for the
rerun was prepared.

## Attribution and license

DolphinBench is by Mem0, under the Apache License 2.0. This folder imports its runner, adapters and
grader at run time, and the records in `results/` carry its histories, tests, app data and grading
checks, which keep its terms. Hermes Agent is by Nous Research, under the MIT License. See
[NOTICE.md](NOTICE.md). Past Corp's code here is MIT licensed, like the rest of this repository.
