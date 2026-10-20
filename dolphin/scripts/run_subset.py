"""Runs a chosen subset of tests on a copy of the finished run (experiment only).
EXP_TESTS="morgan:135,alex:012" python run_subset.py --config <copy>.yaml --confirm-paid-calls"""
import os, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
import harness.runner as runner
from harness.durable_json import save_json
from turn_recovery import drop_unsent_defaults, execute_with_reruns

if os.environ.get("EXP_TOOLSETS"):
    # The official DolphinBench runs gave Hermes only these toolsets (their launch manifests).
    import harness.adapters.hermes as hermes_adapter
    hermes_adapter.TOOLSETS[:] = os.environ["EXP_TOOLSETS"].split(",")

WANTED = {(p, i.zfill(3)) for p, i in (x.split(":") for x in os.environ["EXP_TESTS"].split(","))}


def evaluate_subset(self):
    self.adapter.verify_checkpoint = lambda persona, checkpoint: None  # profiles edited for the experiment
    original = self.adapter.run_interaction

    def run_interaction(request):
        record = original(request)
        drop_unsent_defaults(self.directory, request.persona, request.interaction_id, record)
        return record

    self.adapter.run_interaction = run_interaction

    def one(persona, spec):
        evidence = execute_with_reruns(self, persona, spec, "tests")
        grade_path = self._path("grades", persona, str(spec["id"]).zfill(3))
        if not grade_path.exists():
            save_json(grade_path, self._grade(spec, evidence))

    work = [(p, s) for p, r in self.release.items() for s in r["tests"] if (p, str(s["id"]).zfill(3)) in WANTED]
    print(f"running {len(work)} tests", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in [pool.submit(one, p, s) for p, s in work]:
            future.result()


runner.Runner.evaluate = evaluate_subset

# Experiment copies only: the copy's adapter identity names the copy's own
# paths, so the comparison with the original run's identity file is skipped here.
_init = runner.Runner.__init__


class _Equal:
    def __ne__(self, other):
        return False


def _init_on_copy(self, *args, **kwargs):
    real = runner.load_json
    runner.load_json = lambda data: _Equal() if b'"release"' in data[:200] else real(data)
    try:
        _init(self, *args, **kwargs)
    finally:
        runner.load_json = real


runner.Runner.__init__ = _init_on_copy
raise SystemExit(runner.main(["evaluate", *sys.argv[1:]]))
