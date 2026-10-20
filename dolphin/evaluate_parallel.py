"""DolphinBench's evaluate stage with several tests at once.

    python evaluate_parallel.py --config past-run.yaml --confirm-paid-calls [--workers 6]

Each test is independent: a fresh conversation, its own app state, and the frozen memory, which
no test changes. The runner evaluates them one after another; this runs the same stage with a pool
of threads, each test executed and graded exactly as the runner does, every record, grade and cost
file the runner's own. Checkpoints are verified before any test starts, as in the runner, and the
cost is collected once every test is graded. Resuming works with either.
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))  # run from the DolphinBench checkout, as the runner is

import harness.runner as runner  # noqa: E402
from harness.durable_json import save_json  # noqa: E402
from harness.submission import load_json  # noqa: E402
from turn_recovery import drop_unsent_defaults, execute_with_reruns  # noqa: E402

WORKERS = 6


def evaluate_in_parallel(self):
    for persona in self.release:
        path = self.directory / "checkpoints" / f"{persona}.json"
        if not path.exists():
            raise ValueError(f"Complete ingestion first: {persona}")
        self.adapter.verify_checkpoint(persona, load_json(path.read_bytes()))
    self._saved_cost("ingestion")

    def one(persona, spec):
        evidence = execute_with_reruns(self, persona, spec, "tests")  # an unexportable test runs again
        grade_path = self._path("grades", persona, str(spec["id"]).zfill(3))
        if not grade_path.exists():
            save_json(grade_path, self._grade(spec, evidence))

    original = self.adapter.run_interaction

    def run_interaction(request):
        record = original(request)
        drop_unsent_defaults(self.directory, request.persona, request.interaction_id, record)
        return record

    self.adapter.run_interaction = run_interaction

    work = [(persona, spec) for persona, release in self.release.items() for spec in release["tests"]]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(one, persona, spec) for persona, spec in work]
        try:
            for future in futures:
                future.result()
        except BaseException:
            # The first failure stops the stage: tests not started are dropped, finished ones kept.
            pool.shutdown(wait=True, cancel_futures=True)
            raise
    self._collect_cost("tests")


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if "--workers" in arguments:
        index = arguments.index("--workers")
        WORKERS = int(arguments[index + 1])
        del arguments[index:index + 2]
    # A local override, read when the stage starts: ~/.dolphin-hermes/evaluate-workers holds a number.
    override = Path.home() / ".dolphin-hermes" / "evaluate-workers"
    if override.is_file():
        WORKERS = int(override.read_text().strip())
    runner.Runner.evaluate = evaluate_in_parallel
    raise SystemExit(runner.main(["evaluate", *arguments]))
