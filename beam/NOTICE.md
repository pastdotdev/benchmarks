# Notices

This harness runs a benchmark and uses prompts that belong to others. It downloads the full
dataset when it runs, includes dataset-derived content in the published results, and includes
ExaBase's prompts unchanged.

**BEAM**
- From "Beyond a Million Tokens: Benchmarking and Enhancing Long-Term Memory in LLMs" by Mohammad
  Tavakoli, Alireza Salemi, Carrie Ye, Mohamed Abdalla, Hamed Zamani and J Ross Mitchell
  ([arXiv:2510.27246](https://arxiv.org/abs/2510.27246)).
- The dataset ([Mohammadta/BEAM](https://huggingface.co/datasets/Mohammadta/BEAM) and
  [Mohammadta/BEAM-10M](https://huggingface.co/datasets/Mohammadta/BEAM-10M)) is published under
  [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). The harness downloads it from
  Hugging Face and records the revision it used.
- The published `results/{split}/results.jsonl` files include dataset questions, reference
  answers, and retrieved conversation excerpts, alongside model answers and judge verdicts.
  The dataset-derived content retains its CC BY-SA 4.0 terms and is excluded from this
  repository's MIT license. These evaluation records select and reformat dataset material
  and add model-generated content; they are not the original dataset files.

**ExaBase's BEAM prompts**
- `exabase_prompts.py` contains the answer and judge prompts from ExaBase's published BEAM
  adapter ([source](https://fabric.so/p/beam-3VjBcqEVRofZyA5CeazeX)). That file's SHA-256 is
  `584b672d957f3e6f75e96f881ec31da22e1039da1df5fbedb53dc6a9145cfc43`, and
  `python verify.py --prompts` confirms our copy matches it.
- Credit for the answer and judge prompts belongs to ExaBase. We reproduce them unchanged
  to use the same prompting and evaluation methodology as its published BEAM adapter.
- The ExaBase-derived portions of `exabase_prompts.py` remain ExaBase's work and are excluded
  from this repository's MIT license. This attribution does not relicense those portions.

Past Corp's original harness code is covered by the repository's MIT license. The third-party
material identified above retains its separate terms.
