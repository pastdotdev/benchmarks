"""DolphinBench's ingest stage with the three personas at once.

    python ingest_parallel.py --config past-run.yaml --confirm-paid-calls

The runner ingests one persona after another and freezes each before the next starts. The
personas are independent, one memory each, so this runs the same stage with one thread per
persona: each persona's messages still go to the agent in their order, each is frozen once its
memory is complete, and every record, checkpoint and cost file is the runner's own. Only the loop
over personas changes. With past.dev, whose project processes its pushes one after another, it
cuts the stage to about the longest persona's time. Use the runner's `ingest` for the official
sequential order; resuming works with either.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))  # run from the DolphinBench checkout, as the runner is

import harness.runner as runner  # noqa: E402
from harness.durable_json import save_json  # noqa: E402
from harness.submission import load_json  # noqa: E402
from turn_recovery import await_processed, execute_with_reruns, freeze_disclosing  # noqa: E402


def ingest_in_parallel(self):
    errors = []

    def one(persona, release):
        try:
            checkpoint = self.directory / "checkpoints" / f"{persona}.json"
            if checkpoint.exists():
                self.adapter.verify_checkpoint(persona, load_json(checkpoint.read_bytes()))
                return
            for spec in release["sessions"]:
                execute_with_reruns(self, persona, spec)  # a turn Hermes could not complete runs again
            self.adapter.past._flush(persona)
            await_processed(self.adapter, persona)  # failed past.dev pushes are retried, not fatal
            save_json(checkpoint, freeze_disclosing(self.adapter, persona))  # empty pushes listed, not fatal
        except BaseException as error:  # reported once every persona has stopped
            errors.append((persona, error))

    # Daemon threads, so a stop signal ends the process and its keys are revoked on the way out.
    threads = [threading.Thread(target=one, args=item, name=item[0], daemon=True) for item in self.release.items()]
    for thread in threads:
        thread.start()
    # A persona that fails stops the stage at once rather than after the others finish, hours
    # later; the others' completed turns are kept and the stage resumes from them.
    while any(thread.is_alive() for thread in threads) and not errors:
        time.sleep(5)
    if errors:
        persona, error = errors[0]
        raise ValueError(f"{persona}: {error}") from error
    self._collect_cost("ingestion")


if __name__ == "__main__":
    runner.Runner.ingest = ingest_in_parallel
    raise SystemExit(runner.main(["ingest", *sys.argv[1:]]))
