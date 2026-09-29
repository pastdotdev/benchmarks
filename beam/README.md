# BEAM on past.dev

This harness runs the [BEAM](https://arxiv.org/abs/2510.27246) long-conversation memory benchmark
against past.dev's public API. It loads the dataset, ingests each conversation, waits for
past.dev to process it, then asks the benchmark's questions and scores the answers.

The answer and judge prompts are ExaBase's published BEAM prompts, unchanged, so scores compare
with other published BEAM results. Everything else is ordinary use of the public API.

## Results

The saved baseline covers all 100 conversations and 2,000 questions. The combined score,
weighted by question count, is **90.02%**.

| Split | Conversations | Questions | Score | Files |
|---|---:|---:|---:|---|
| 100K | 20 | 400 | **92.08%** | [Summary](results/100k/summary.json) · [Results](results/100k/results.jsonl) |
| 500K | 35 | 700 | **89.63%** | [Summary](results/500k/summary.json) · [Results](results/500k/results.jsonl) |
| 1M | 35 | 700 | **90.65%** | [Summary](results/1m/summary.json) · [Results](results/1m/results.jsonl) |
| 10M | 10 | 200 | **85.03%** | [Summary](results/10m/summary.json) · [Results](results/10m/results.jsonl) |

| Category | 100K | 500K | 1M | 10M |
|---|---:|---:|---:|---:|
| Abstention | 98.75% | 92.14% | 97.14% | 100.00% |
| Contradiction resolution | 78.13% | 78.57% | 75.36% | 73.13% |
| Event ordering | 100.00% | 100.00% | 99.81% | 100.00% |
| Information extraction | 95.00% | 86.32% | 83.98% | 68.75% |
| Instruction following | 96.25% | 92.02% | 96.43% | 95.00% |
| Knowledge update | 89.38% | 86.07% | 85.71% | 92.50% |
| Multi-session reasoning | 68.95% | 71.39% | 72.82% | 28.17% |
| Preference following | 100.00% | 99.05% | 98.87% | 100.00% |
| Summarization | 99.38% | 97.25% | 99.60% | 99.00% |
| Temporal reasoning | 95.00% | 93.45% | 96.79% | 93.75% |

Each split includes a summary and per-question results with answers, evidence, scores, and
judge verdicts. Use `verify.py` to check the scores against the saved verdicts.

## Run it

You need Python 3.11 or newer.

```bash
pip install -r requirements.txt
export PAST_API_URL=https://api.past.dev
export PAST_MANAGEMENT_KEY=past_mk_...        # Settings › Organization key
export OPENROUTER_API_KEY=...
python run.py --split 100k                    # 100k, 500k, 1m or 10m
python run.py --split 100k --conversations 5  # only the first five conversations
```

- **Projects.** Each conversation gets its own project: 20 at 100K, 35 at 500K and 1M, 10 at 10M.
  If your account's project limit is lower, use `--conversations N`. The harness never deletes
  projects.
- **Dataset.** Each split downloads from Hugging Face once and is cached in `~/.cache/beam-open`
  (set `BEAM_OPEN_CACHE` to move it). The 10M split is about 1 GiB.
- **New run results** go to `results/beam-<split>/`. `results.jsonl` has one line per question, with the
  recall response, the answer and the judge's verdicts. `summary.json` has the scores, the dataset
  revision and the models used.
- **Resuming.** Run the same command again. Conversations already scored are skipped, and one that
  was cut off starts over in a new project.

### Score again without reingesting

Each run tags its projects with its start time, shown in the first log line and in
`summary.json`. Pass the tag to recall and score the same projects again:

```bash
python run.py --split 100k --reuse 0923-164412 --output results/beam-100k-rescore
```

Nothing is ingested again. The harness waits until the projects are processed, asks every question,
and removes the temporary keys it created. To find what to wait on, it resends each conversation's
last batch, which past.dev recognizes by its idempotency key and neither ingests nor charges again.
Use the same harness code and dataset revision as the original run. Changes to ingestion
payloads change batch idempotency keys and require fresh projects. If replaying a batch returns
an idempotency conflict, the compatibility fallback assumes the project has finished processing;
it cannot verify the original push's status.

## Check a run

```bash
python verify.py results/100k        # verify the published 100K results
python verify.py --prompts           # the prompts match ExaBase's published file
```

The first check makes no Past or model API calls. It loads the public dataset (downloading it if
not already cached), confirms that every expected question has one record, checks each score
against the saved judge verdicts, and confirms that the summary is their mean. It verifies the
recorded scoring arithmetic; it does not rerun retrieval, answer generation, or judging. The second
downloads ExaBase's adapter, checks its SHA-256 (`584b672d…fc43`) and confirms our prompts match it.

## How answers are scored

- The answer model reads the question and past.dev's recall response, as returned, inside
  ExaBase's answer prompt.
- The judge is ExaBase's version of the BEAM paper's judge (`scoring.py`):
  - **Event ordering:** the answer's list is matched to the reference topics, and the two orders
    are compared with Kendall tau-b, scaled from 0 to 1.
  - **Every other category:** each rubric item scores 0, 0.5 or 1, and the question scores their
    average.
  - The headline is the mean over all questions. The summary also gives each category's mean.
- One model answers and judges: `openai/gpt-5.6-luna` through OpenRouter, with reasoning off. Each
  question is answered once per harness invocation.
- A call that still fails after retries scores 0, as in ExaBase's adapter, and `summary.json`
  counts these. Only an account error, such as a wrong key or no credits, stops the run.

## How conversations are sent

- **Identities.** The user's identity is their name plus the conversation id (`craig-baker-3`),
  with their profile as traits. The assistant is `assistant`. Each turn is sent as its author, and
  every question is asked as the user.
- **Turns.** Each turn carries a source ID, content, label, timestamp, and identity. Its metadata
  contains only role, author, and speaker. Turns go in pushes of up to 100
  (`POST /api/v1/ingest/batch`), in order. BEAM
  dates sessions by day, so each turn is placed at noon UTC on its day, one microsecond after the
  turn before it. Nothing sent names the benchmark except the project and key names.
- **Waiting.** The harness polls each conversation's last push (`GET /api/v1/ingest/{id}`) until it
  is `completed` and the project is `settled`. A push that fails stops the run. The 10M split takes
  hours.
- **Asking.** Recall runs at the moment the question is asked (`queryTimestamp`): the end of the
  conversation, or for knowledge-update questions, the update the dataset cites. Each recall asks
  for up to 100 results within 8,000 tokens of evidence.

## Files

| File | What it does |
|---|---|
| `run.py` | the whole run, one function per step |
| `dataset.py` | loads BEAM and turns each conversation into messages and questions |
| `past_api.py` | the past.dev API calls |
| `exabase_prompts.py` | ExaBase's answer and judge prompts |
| `llm.py` | the answer and judge model |
| `scoring.py` | scoring and the summary |
| `log.py` | logging |
| `verify.py` | checks a run's numbers and the prompts |
| `NOTICE.md` | credits for the BEAM dataset and ExaBase's prompts |

## Attribution and license

The answer and judge prompts are ExaBase's work, reproduced unchanged from its
[published BEAM adapter](https://fabric.so/p/beam-3VjBcqEVRofZyA5CeazeX).
Past Corp's original harness code is covered by the repository's [MIT license](../LICENSE).
The ExaBase-derived prompts are excluded from that license; see [NOTICE.md](NOTICE.md)
for their attribution and the BEAM dataset's separate terms.
