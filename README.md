# past.dev benchmarks

The harnesses for the benchmarks reported on [past.dev/benchmarks](https://past.dev/benchmarks),
published so anyone can read the method and run it. They use only past.dev's public API, the same
calls you would make to add memory to your own product.

| Folder | Benchmark |
|---|---|
| [`beam/`](beam) | [BEAM](https://arxiv.org/abs/2510.27246), long-conversation memory at 100K, 500K, 1M and 10M tokens |

To run one, you need a past.dev account ([sign up](https://sso.past.dev/sign-up)), its
organization key, and an [OpenRouter](https://openrouter.ai) key. Each conversation gets its own
project, and a new account allows fewer projects than a full split needs; the folder's README says
how to run within that.

## BEAM results

The saved baseline covers all 100 conversations and 2,000 questions. The combined score,
weighted by question count, is **90.02%**.

| Split | Conversations | Questions | Score | Files |
|---|---:|---:|---:|---|
| 100K | 20 | 400 | **92.08%** | [Summary](beam/results/100k/summary.json) · [Results](beam/results/100k/results.jsonl) |
| 500K | 35 | 700 | **89.63%** | [Summary](beam/results/500k/summary.json) · [Results](beam/results/500k/results.jsonl) |
| 1M | 35 | 700 | **90.65%** | [Summary](beam/results/1m/summary.json) · [Results](beam/results/1m/results.jsonl) |
| 10M | 10 | 200 | **85.03%** | [Summary](beam/results/10m/summary.json) · [Results](beam/results/10m/results.jsonl) |

See the [BEAM README](beam/README.md#results) for the category breakdown and instructions
for verifying the saved results or running the benchmark.

## License

The code is MIT licensed ([LICENSE](LICENSE)). Benchmarks, datasets and third-party prompts keep
their own terms, credited in each folder's `NOTICE.md`.
