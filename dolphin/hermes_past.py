"""DolphinBench's Hermes + Luna agent, with past.dev as its memory provider.

The paper's leaderboard compares memory systems inside one agent: Hermes, pinned to
NousResearch/hermes-agent 935c1e8 with DolphinBench's evaluation patch, on Azure `gpt-5.6-luna`
at high reasoning. Mem0, Honcho, Hindsight and Supermemory are Hermes memory plugins. This adapter
runs that same agent with one more plugin, past.dev's (`plugins/memory/past` in the Hermes
checkout), and changes nothing else:

- Hermes, its profile and its isolated container are DolphinBench's: the adapter is its Hermes
  adapter (`harness.adapters.hermes`), with the profile its reference builds for Hermes + Luna and
  `memory.provider: past`. The checkout must be the pinned base, DolphinBench's patch, and only the
  plugin added; `identity()` checks that and records the image.
- Ingestion. Every history message goes through a Hermes turn, as on the leaderboard. The plugin
  keeps the completed turn (the message and Hermes' reply) in the profile's spool; after the turn,
  this adapter pushes the spool to past.dev, 100 data points to a push, with the message's date and
  author. A turn whose memory write is missing stops the run.
- Freeze. Every push processed (`past_harness.PastHarness.freeze`), then the profile is frozen as
  DolphinBench's adapter does. Tests run on copies of the frozen profile, with memory read-only:
  the plugin then only recalls, and `verify_checkpoint` reads the project's request log.
- Cost. Every model call of a turn goes through the agent relay (agent_relay.py) tagged with the
  turn; a turn costs what OpenRouter billed for its calls, failed attempts included (Hermes records
  no price for a relayed endpoint). Plus the credits past.dev billed, at list price.

On Docker Desktop (macOS), set DOLPHINBENCH_DOCKER_CONTEXT=desktop-linux: DolphinBench's Docker
commands then name that context instead of `default`, which has no daemon there, and the agent
container starts before its payload is copied in, because Docker Desktop forwards the app socket
only into a started container (see `use_docker_context`). The container's flags, single read-only
mount, environment and command are DolphinBench's.

Environment: PAST_API_URL and PAST_MANAGEMENT_KEY (this host), PAST_AGENT_API_URL (past.dev as the
agent's container reaches it), DOLPHINBENCH_AGENT_IMAGE, AZURE_OPENAI_BASE_URL and
AZURE_FOUNDRY_API_KEY (the agent's model endpoint), DOLPHINBENCH_HERMES_SOURCE.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import MutableMapping
from contextlib import contextmanager
from pathlib import Path

import yaml

if __name__ == "__main__":
    sys.path.insert(0, str(Path.cwd()))  # run from the DolphinBench checkout, as the runner is

import harness  # noqa: E402
from harness import environment as harness_environment
from harness import hermes_driver
from harness.adapters.hermes import READ_ONLY, TOOLSETS, HermesAdapter, copy_profile
from harness.adapters.hermes_profile import _load_personas, _profile_config, toolsets_for
from harness.durable_json import save_json
from harness.submission import check_total_cost, load_json

from past_harness import PERSONAS, PastHarness, append_line, read_json, read_lines, utc_iso

ADAPTER_VERSION = "1.0"
ROOT = Path(harness.__file__).resolve().parents[1]
SPEC_PATH = ROOT / "examples/reference/hermes-builtin.json"
PLUGIN = Path("plugins/memory/past")
DIFF_FLAGS = ["--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--no-renames",
              "--src-prefix=a/", "--dst-prefix=b/", "--unified=3"]
OPTIONS = {"source_env", "image_env", "base_url_env", "agent_api_url_env", "credit_usd_per_1000"}
# Hermes installs these on its first turn (`tools.lazy_deps`: provider.bedrock, tts.edge). Every turn
# starts a fresh container from the image, so without them every turn downloads and installs both
# again, about 15 seconds. The image built here has them already, at the versions Hermes installs.
LAZY_DEPENDENCIES = ("boto3==1.42.89", "edge-tts==7.2.7")
# Azure's content filter sometimes cuts a harmless Luna answer off mid-stream; Hermes reports the
# turn as a refusal. Such a turn is run again from the state before it, as many times as this.
CONTENT_FILTER_RERUNS = 5


class ContentFiltered(Exception):
    """The model provider's content filter cut the turn off."""


def tool_results(state_db: Path, session_id: str) -> dict:
    """The tool results Hermes kept for a session, by call id."""
    import sqlite3
    with sqlite3.connect(f"{state_db.resolve().as_uri()}?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT tool_call_id, content FROM messages WHERE session_id = ? AND role = 'tool' "
                                  "AND tool_call_id IS NOT NULL ORDER BY id", (session_id,)).fetchall()
    return {call_id: content for call_id, content in rows}


def empty_answer_usage(tag: str) -> dict | None:
    """The usage OpenRouter reported for the turn's empty answer, in the record's usage form."""
    for record in reversed(read_lines(Path(os.environ.get("AGENT_RELAY_TAGS", ".")) / f"{tag}.jsonl")):
        if record.get("event") == "end" and record.get("empty_completion") == "first" and record.get("usage"):
            usage = record["usage"]
            prompt = int(usage.get("input_tokens") or 0)
            details = usage.get("input_tokens_details") or {}
            cached, written = int(details.get("cached_tokens") or 0), int(details.get("cache_write_tokens") or 0)
            output = int(usage.get("output_tokens") or 0)
            return {"input_tokens": prompt - cached - written, "output_tokens": output,
                    "cache_read_tokens": cached, "cache_write_tokens": written,
                    "reasoning_tokens": int((usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0),
                    "request_count": 1, "prompt_tokens": prompt, "total_tokens": int(usage.get("total_tokens") or prompt + output)}
    return None


def complete_replyless(messages: list, results: dict, usage: dict | None) -> bool:
    """A turn the model ended with an empty answer stops, in Hermes' recording, at its last tool
    calls: the request it answered with nothing held their results, and Hermes records no empty
    answer. The results are appended from Hermes' own session, then the empty answer with the usage
    OpenRouter reported for it, so the record ends as the model ended it."""
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    pending = [call["id"] for m in messages if m.get("role") == "assistant"
               for call in m.get("tool_calls") or [] if call["id"] not in answered]
    if not pending or usage is None or any(call_id not in results for call_id in pending):
        return False
    for call_id in pending:
        messages.append({"role": "tool", "tool_call_id": call_id, "content": results[call_id]})
    messages.append({"role": "assistant", "content": "", "usage": usage})
    return True


def clone_tree(source: Path, target: Path) -> None:
    """A copy of a directory; on macOS an APFS clone, which takes no time and no space."""
    if sys.platform == "darwin":
        subprocess.run(["cp", "-cR", str(source), str(target)], check=True, capture_output=True)
    else:
        shutil.copytree(source, target)


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# --- One environment per thread ---------------------------------------------------------------
#
# DolphinBench's Hermes adapter sets each turn's variables (persona, app state paths, read-only
# flags) in os.environ for the length of the turn. With the three personas ingesting at once
# (ingest_parallel.py), each thread needs its own view, so the driver and its settings reader see a
# per-thread overlay on the process environment. The process environment itself is never changed.

class ThreadEnviron(MutableMapping):
    def __init__(self, base):
        self._base = base
        self._local = threading.local()

    def _overlay(self):
        return getattr(self._local, "values", None)

    def _view(self):
        overlay = self._overlay()
        if overlay is None:
            return self._base
        # A None in the overlay hides the process's value from the turn.
        return {key: value for key, value in {**self._base, **overlay}.items() if value is not None}

    def __getitem__(self, key):
        return self._view()[key]

    def __iter__(self):
        return iter(dict(self._view()))

    def __len__(self):
        return len(self._view())

    def __setitem__(self, key, value):
        overlay = self._overlay()
        (overlay if overlay is not None else self._base)[key] = value

    def __delitem__(self, key):
        overlay = self._overlay()
        del (overlay if overlay is not None else self._base)[key]

    def copy(self):
        return dict(self._view())

    @contextmanager
    def overlay(self, values):
        if self._overlay() is not None:
            raise RuntimeError("Nested turn environments are not supported")
        self._local.values = dict(values)
        try:
            yield
        finally:
            self._local.values = None


class _Module:
    """A module whose attributes are another's, with some replaced."""

    def __init__(self, module, **replaced):
        self._module = module
        self.__dict__.update(replaced)

    def __getattr__(self, name):
        return getattr(self._module, name)


TURN_ENVIRONMENT = ThreadEnviron(os.environ)
hermes_driver.os = _Module(os, environ=TURN_ENVIRONMENT)
harness_environment.os = _Module(os, environ=TURN_ENVIRONMENT)


# --- Docker Desktop (macOS) -------------------------------------------------------------------

def docker_desktop_args(args, context: str):
    """DolphinBench's docker command, against `context` instead of `default`."""
    if not isinstance(args, (list, tuple)) or len(args) < 3 or args[0] != "docker":
        return args
    out = list(args)
    if out[1:3] == ["--context", "default"]:
        out[2] = context
    return out


READY = "/payload/.dolphinbench-ready"
EXITED = "/payload/.dolphinbench-exited"
# The command starts once the payload is in. When it ends, its status is written down and the
# container stays up, its socket still forwarded, until its output has been copied out.
WRAPPER = (f'while [ ! -e {READY} ]; do sleep 0.05; done; "$@"; status=$?; echo $status > {EXITED}; '
           'while :; do sleep 60; done')


def use_docker_context(context: str, socket_dir: Path) -> None:
    """DolphinBench's agent container on Docker Desktop.

    Docker Desktop forwards a host Unix socket into a container only while it runs: `docker cp`
    into a container that has not started makes an empty directory where the socket goes, so the
    start fails, and `docker cp` out of a stopped one refuses the socket that is left. Docker Desktop
    also keeps the macOS user's id on copied files, which the container's capability-less root
    cannot write to. So the container starts first, with DolphinBench's flags, mount, environment
    and command, behind a wrapper (WRAPPER) that waits for the payload, runs the command, and keeps
    the container up until its output has been read back; the payload goes in owned by root, as
    `docker cp` leaves it on Linux. Output, status and logs are read as DolphinBench reads them.
    """
    from reference.execution import agent_container
    from reference.execution import build_agent_image
    from reference.execution import hermes_agent

    socket_dir.mkdir(parents=True, exist_ok=True)

    def docker(*args, **kwargs):
        return subprocess.run(["docker", "--context", context, *args], **kwargs)

    def put_payload(name, source: Path):
        def as_root(info):
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            return info

        copy = subprocess.Popen(["docker", "--context", context, "cp", "-", f"{name}:/payload"],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=copy.stdin, mode="w|") as archive:
                archive.add(str(source), arcname=".", filter=as_root)
        finally:
            copy.stdin.close()
        if copy.wait(timeout=300):
            raise RuntimeError(f"Cannot copy the agent's payload: {copy.stderr.read().decode()[-500:]}")

    def await_status(name, timeout):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            probe = docker("exec", name, "cat", EXITED, capture_output=True, text=True)
            if probe.returncode == 0 and probe.stdout.strip():
                return int(probe.stdout.strip())
            running = docker("inspect", "--format", "{{.State.Running}}", name, capture_output=True, text=True)
            if running.returncode == 0 and running.stdout.strip() == "false":
                raise RuntimeError("The agent container stopped before its command finished")
            # A failed inspect (Docker Desktop busy) is asked again, never read as a stop.
            if deadline is not None and time.monotonic() > deadline:
                raise subprocess.TimeoutExpired(["docker", "start", "--attach", name], timeout)
            time.sleep(1)

    def take_output(name, output):
        errors = tempfile.TemporaryFile()
        copied = subprocess.Popen(["docker", "--context", context, "cp", name + ":/payload/output/.", "-"],
                                  stdout=subprocess.PIPE, stderr=errors)
        try:
            try:
                agent_container.extract_files(copied.stdout, output)
            except Exception as error:
                copied.wait(timeout=60)
                errors.seek(0)
                raise RuntimeError(f"Cannot preserve the agent's output ({error}; docker cp: "
                                   f"{errors.read().decode('utf-8', 'replace')[-500:]})") from error
            if copied.wait(timeout=60):
                raise RuntimeError("Cannot preserve the agent's output")
        finally:
            copied.stdout.close()
            if copied.poll() is None:
                copied.kill()
                copied.wait()

    class DesktopAgentContainer(agent_container.AgentContainer):
        def run(self, output, timeout=None):
            # The home directory is shared with Docker Desktop; macOS caps a socket path at 104 bytes.
            with tempfile.TemporaryDirectory(prefix="db-app-", dir=str(socket_dir)) as raw:
                socket = Path(raw) / "apps.sock"
                bridge = agent_container.AppBridge(socket, self.app_command, self.app_environment)
                thread = threading.Thread(target=bridge.serve_forever, daemon=True)
                thread.start()
                created = False
                try:
                    args = ["create", "--name", self.name,
                            "--network", self.network, "--cap-drop=ALL",
                            "--security-opt=no-new-privileges", "--pids-limit=512",
                            "--memory=8g", "--cpus=2", "--workdir=/payload/work",
                            "--volume", f"{socket}:/run/apps.sock:ro"]
                    for name, value in self.environment.items():
                        if not name or "=" in name or "\x00" in name + value:
                            raise ValueError("Invalid agent environment entry")
                        args += ["--env", f"{name}={value}"]
                    args += ["--entrypoint", "/bin/sh", self.image, "-c", WRAPPER, "sh", *self.command]
                    docker(*args, check=True, capture_output=True, text=True)
                    created = True
                    docker("start", self.name, check=True, capture_output=True)
                    put_payload(self.name, self.payload)
                    marker = Path(raw) / "ready"
                    marker.write_text("")
                    docker("cp", str(marker), f"{self.name}:{READY}", check=True, capture_output=True)
                    try:
                        status = await_status(self.name, timeout)
                        logs = docker("logs", self.name, capture_output=True, text=True)
                        result = subprocess.CompletedProcess(["docker", "start", "--attach", self.name],
                                                             status, logs.stdout, logs.stderr)
                    except subprocess.TimeoutExpired:
                        docker("kill", self.name, capture_output=True)
                        raise
                    finally:
                        # Preserve output even after an interrupted agent turn.
                        take_output(self.name, output)
                        self.output_valid = True
                    return result
                finally:
                    if created:
                        docker("rm", "--force", self.name, capture_output=True)
                    bridge.stop()
                    thread.join(timeout=5)

    hermes_agent.AgentContainer = DesktopAgentContainer

    # A killed run leaves its agent containers running. One runner works a run at a time (the
    # runner's lock), and every container of this kind is a turn of such a run.
    leftovers = docker("ps", "-aq", "--filter", "name=^dolphinbench-agent-", capture_output=True, text=True)
    if leftovers.returncode == 0 and leftovers.stdout.split():
        docker("rm", "--force", *leftovers.stdout.split(), capture_output=True)
    for stale in socket_dir.glob("db-app-*"):
        shutil.rmtree(stale, ignore_errors=True)

    def run(args, *rest, **kwargs):
        return subprocess.run(docker_desktop_args(args, context), *rest, **kwargs)

    build_agent_image.subprocess = _Module(subprocess, run=run)
    # AgentContainer refuses DOCKER_HOST and DOCKER_CONTEXT, which stay unset: each command names the context.


if sys.platform == "darwin":
    # macOS's temporary directory is under /var, a symlink to /private/var, and DolphinBench's
    # launcher refuses an output path under a symlink.
    tempfile.tempdir = os.path.realpath(tempfile.gettempdir())

if os.environ.get("DOLPHINBENCH_DOCKER_CONTEXT"):
    use_docker_context(os.environ["DOLPHINBENCH_DOCKER_CONTEXT"],
                       Path(os.environ.get("DOLPHINBENCH_SOCKET_DIR", Path.home() / ".dbtmp")))


# --- The Hermes checkout ----------------------------------------------------------------------

def verify_source(source: Path, spec: dict) -> dict:
    """The pinned Hermes base, DolphinBench's evaluation patch, and only past.dev's plugin added."""
    def git(*args) -> bytes:
        return subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True, timeout=60).stdout

    if git("status", "--porcelain", "--untracked-files=all").strip():
        raise ValueError("The Hermes checkout has uncommitted changes")
    head = git("rev-parse", "HEAD").decode().strip()
    patch = git("diff", *DIFF_FLAGS, spec["source_base"], head, "--", ".", f":(exclude){PLUGIN}")
    if sha256(patch) != spec["patch_sha256"]:
        raise ValueError("Outside plugins/memory/past, the Hermes checkout differs from DolphinBench's reference")
    plugin = {name: sha256((source / PLUGIN / name).read_bytes()) for name in ("__init__.py", "plugin.yaml")}
    return {"source_base": spec["source_base"], "patch_sha256": spec["patch_sha256"], "head": head,
            "plugin": plugin}


def docker_image_id(image: str) -> str:
    context = os.environ.get("DOLPHINBENCH_DOCKER_CONTEXT") or "default"
    found = subprocess.run(["docker", "--context", context, "image", "inspect", "--format", "{{.Id}}", image],
                           check=True, capture_output=True, text=True, timeout=60).stdout.strip()
    if found != image:
        raise ValueError(f"Docker image {image} is not available")
    return found


class HermesPastAdapter(HermesAdapter):
    def __init__(self, options, work_dir):
        unknown = set(options) - OPTIONS
        if unknown or not {"source_env", "image_env", "base_url_env", "agent_api_url_env"} <= set(options):
            raise ValueError(f"Options are {sorted(OPTIONS)}; unknown: {sorted(unknown)}")
        values = {}
        for key in ("source", "image", "base_url", "agent_api_url"):
            values[key] = os.environ.get(options[f"{key}_env"], "").strip()
            if not values[key]:
                raise ValueError(f"Set {options[f'{key}_env']} before running")
        self.home = Path(work_dir) / "hermes"
        self.profiles = self.home / "profiles"
        self.reference = None
        self.templates = {}
        self.spec = json.loads(SPEC_PATH.read_text())
        self.source = Path(values["source"]).resolve()
        self.source_identity = verify_source(self.source, self.spec)
        self.image = docker_image_id(values["image"])
        self.agent_api_url = values["agent_api_url"].rstrip("/")
        self.binary = str(self.source / "hermes")  # identity only: the turn runs the image's Hermes
        self.app_python = sys.executable
        self.agent = {**self.spec["agent"], "base_url": values["base_url"]}
        self.past = PastHarness({"credit_usd_per_1000": options.get("credit_usd_per_1000", 0.30),
                                 "release_root": str(ROOT)}, Path(work_dir))
        self._key_lock = threading.Lock()
        self._replyless = {}  # (phase, persona, id) -> tool results of a turn the model ended with no answer
        self.generated_templates = {}
        for persona, name in _load_personas(ROOT).items():
            directory = self.profiles / persona
            job = {"id": f"past-{persona}", "configuration": "builtin", "persona": persona,
                   "toolsets": toolsets_for("builtin"), "provider_identity": {},
                   "run_id": self.home.parent.name, "state_path": str(directory / "state.json"),
                   "log_path": str(directory / "calls.jsonl"), "tool_config_path": str(directory / "tools.json")}
            manifest = {"root": str(ROOT), "agent": self.agent, "launch": {"mock_mcp_python": self.app_python}}
            config = _profile_config(job, manifest, {})
            config["memory"]["provider"] = "past"
            soul = (ROOT / "harness/benchmark_soul.md").read_text().replace("{{persona_name}}", name)
            self.generated_templates[persona] = {"config.yaml": yaml.safe_dump(config, sort_keys=False),
                                                 "SOUL.md": soul}
        hermes_driver.HERMES_BIN = self.binary

    def identity(self):
        here = Path(__file__).resolve().parent
        past = self.past.identity()
        return {
            "harness": "Hermes + past.dev", "adapter_version": ADAPTER_VERSION,
            "code": {name: sha256((here / name).read_bytes())
                     for name in ("hermes_past.py", "past_harness.py", "past_client.py")},
            "hermes": self.source_identity, "agent_image": self.image, "agent": self.agent,
            "reference_sha256": sha256(SPEC_PATH.read_bytes()),
            "profile_builder_sha256": sha256((ROOT / "harness/adapters/hermes_profile.py").read_bytes()),
            "templates": {p: {name: sha256(content.encode()) for name, content in files.items()}
                          for p, files in self.generated_templates.items()},
            "retry_environment": {name: os.environ.get(name, default) for name, default in (
                ("DOLPHINBENCH_SEED_RETRIES", "3"), ("DOLPHINBENCH_TEST_RETRIES", "3"),
                ("DOLPHINBENCH_RETRY_BASE_SECONDS", "5"), ("DOLPHINBENCH_RETRY_MAX_SECONDS", "60"))},
            "docker_context": os.environ.get("DOLPHINBENCH_DOCKER_CONTEXT") or "default",
            "image_preinstalled": list(LAZY_DEPENDENCIES),
            "model_route": {"relay": "agent_relay.py", "relay_sha256": sha256((here / "agent_relay.py").read_bytes()),
                            "upstream": "OpenRouter openai/gpt-5.6-luna, provider azure, no fallback"},
            "memory": {
                "provider": "past.dev", "api_host": past["memory"]["api_host"],
                "agent_api_url": self.agent_api_url,
                "ingestion": {"stored": "each completed Hermes turn: the message, with its date, local time and "
                                        "author, and Hermes' reply", "items_per_push": self.past.options["ingest_batch"]},
                "recall": {**{k: past["memory"]["recall"][k] for k in ("limit", "maxTokens")},
                           "automatic": False, "tool": "past_recall", "queries_per_call": 1},
                "credits": past["memory"]["credits"],
                "internal_models": past["memory"]["internal_models"],
                "credit_usd_per_1000": past["memory"]["credit_usd_per_1000"],
            },
        }

    # --- Profiles -----------------------------------------------------------------------------

    def _initialize(self, persona):
        super()._initialize(persona)
        config = self.profiles / persona / "past.json"
        if not config.exists():
            key = self._agent_key(persona)
            temporary = config.with_suffix(".writing")
            temporary.write_text(json.dumps({"base_url": self.agent_api_url, "api_key": key,
                                             "identity": self.past._identity(persona)}))
            os.chmod(temporary, 0o600)
            temporary.rename(config)

    def _agent_key(self, persona) -> str:
        """The key the agent's plugin recalls (and, while ingesting, nothing else) with: one per
        project for the whole run, kept with the run's files, since frozen profiles carry it."""
        with self._key_lock:
            path = self.past.state / "agent-keys.json"
            keys = read_json(path, {})
            if persona not in keys:
                _, slug = self.past._project(persona)
                key = self.past._management_client().create_project_key(slug, "dolphinbench-hermes")
                keys[persona] = {"project": slug, "id": key["id"], "key": key["key"]}
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".writing")
                temporary.write_text(json.dumps(keys))
                os.chmod(temporary, 0o600)
                temporary.rename(path)
            return keys[persona]["key"]

    # --- Interactions -------------------------------------------------------------------------

    def run_interaction(self, request):
        """DolphinBench's Hermes interaction. A turn the provider's content filter cuts off is run
        again from the state before it (Hermes profile, app state and call log); the cut-off
        attempts are kept under `hermes/filtered/`, and their cost is counted."""
        self.past._mark_phase(request.persona, f"{'ingestion' if request.phase == 'ingestion' else 'tests'}_start")
        state_path = Path(request.apps["env"]["DOLPHINBENCH_STATE_PATH"])
        log_path = Path(request.apps["env"]["DOLPHINBENCH_LOG_PATH"])
        state = state_path.read_bytes() if state_path.exists() else None
        for rerun in range(CONTENT_FILTER_RERUNS + 1):
            try:
                record = super().run_interaction(request)
                replyless = self._replyless.pop((request.phase, request.persona, request.interaction_id), None)
                if replyless is not None and complete_replyless(record.messages, *replyless):
                    append_line(self.past.state / "replyless-records.jsonl",
                                {"phase": request.phase, "persona": request.persona, "id": request.interaction_id})
                break
            except ContentFiltered as filtered:
                self._set_aside(request, str(filtered))
                if rerun == CONTENT_FILTER_RERUNS:
                    raise RuntimeError(f"{request.persona}/{request.interaction_id}: cut off by the content "
                                       f"filter {rerun + 1} times") from filtered
                if state is not None:
                    state_path.write_bytes(state)
                log_path.write_text("")
        if request.phase == "ingestion":
            self._push_turn(request)
        return record

    def _set_aside(self, request, reason):
        """A content-filtered interaction's journal and costs move out of the way of its rerun."""
        target = self.home / "filtered" / request.phase / request.persona / f"{request.interaction_id}-{uuid.uuid4().hex[:8]}"
        target.mkdir(parents=True)
        journal = self.home / "attempts" / request.phase / request.persona / f"{request.interaction_id}.json"
        if journal.exists():
            os.replace(journal, target / "attempts.json")
        for cost in (self.home / "costs" / request.phase / request.persona).glob(f"{request.interaction_id}-*.json"):
            os.replace(cost, target / cost.name)
        save_json(target / "filtered.json", {"reason": reason[:2000]})
        print(f"{request.persona}/{request.interaction_id}: content filter, running the turn again", file=sys.stderr)

    def _push_turn(self, request):
        """The plugin's spooled turn for this message becomes two data points, queued for past.dev.

        The spool then leaves the profile for the run's records, and so do the turn's traces, which
        the runner has already read: otherwise every later turn and every test copies the whole
        history into and out of its container, and the tests' profiles hold it as a plain file."""
        persona = request.persona
        seed = f"{persona}-{request.interaction_id}"
        spool = self.profiles / persona / "past" / "spool.jsonl"
        spooled = read_lines(spool)
        turns = [turn for turn in spooled if turn.get("seed_item_id") == seed]
        if not turns:
            # The model may answer nothing at all, typically after saving a memory; the relay then
            # stops Hermes' retries (agent_relay.py) and the turn ends without a reply, so the
            # plugin, which keeps completed turns, has nothing. The message is still the user's.
            journal = self.home / "attempts" / "ingestion" / persona / f"{request.interaction_id}.json"
            last = load_json(journal.read_bytes())[-1] if journal.exists() else {}
            if "empty_completion" not in str(last.get("driver_error") or ""):
                raise RuntimeError(f"{seed}: Hermes completed the turn but past.dev's plugin kept nothing; see {spool}")
            append_line(self.past.state / "replyless-turns.jsonl", {"seed_item_id": seed, "error": last["driver_error"][:500]})
            turns = [{"seed_item_id": seed, "assistant": ""}]
        for turn in spooled:
            append_line(self.past.state / "turns" / f"{persona}.jsonl", turn)
        reply = str(turns[-1].get("assistant") or "").strip()
        name = PERSONAS[persona]
        common = {"timestamp": utc_iso(request.narrative_time), "identity": self.past._identity(persona)}
        items = [{"id": f"{seed}-user", "content": request.message, "label": f"{name}, message {request.interaction_id}",
                  **common, "metadata": {"role": "user", "author": name, "localTime": request.narrative_time}}]
        if reply:
            items.append({"id": f"{seed}-assistant", "content": reply,
                          "label": f"Assistant's reply to {name}, message {request.interaction_id}", **common,
                          "metadata": {"role": "assistant", "author": "Assistant", "localTime": request.narrative_time,
                                       "inReplyTo": f"{seed}-user"}})
        queue_path = self.past.state / "queue" / f"{persona}.json"
        queue = read_json(queue_path, [])
        known = {item["id"] for item in queue} | self.past._pushed_ids(persona)
        fresh = [item for item in items if item["id"] not in known]
        if fresh:
            save_json(queue_path, queue + fresh)
        spool.unlink(missing_ok=True)
        traces = self.profiles / persona / "submission-traces"
        archive = self.home / "ingestion-traces" / persona
        archive.mkdir(parents=True, exist_ok=True)
        for trace in traces.glob("*.jsonl"):
            os.replace(trace, archive / trace.name)
        if len(queue) + len(fresh) >= self.past.options["ingest_batch"]:
            try:
                self.past._flush(persona)
            except Exception as error:  # the turn is recorded and queued; the next turn or freeze pushes it
                print(f"past.dev: push for {persona} failed, kept queued: {error}", file=sys.stderr)

    def _attempt(self, request, number):
        """DolphinBench's Hermes turn, with the turn's variables in this thread's environment."""
        self._initialize(request.persona)
        profile = request.persona
        if request.phase == "tests":
            profile = f"{request.persona}-test-{request.interaction_id}-a{number:02d}"
            destination = self.profiles / profile
            archive = self.home / "test-traces" / request.persona / f"{request.interaction_id}.jsonl"
            if destination.exists() or archive.exists():
                raise ValueError("Test profile already exists; refusing to replay")
            copy_profile(self.profiles / f"{request.persona}-frozen", destination)
        directory = self.profiles / profile
        config_path = directory / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        config["mcp_servers"]["dolphinbench-apps"].update(request.apps)
        config_path.write_text(yaml.safe_dump(config, sort_keys=False))
        values = {
            **request.apps["env"], "HERMES_HOME": str(self.home),
            "DOLPHINBENCH_CAPTURE_SUBMISSION": "1",
            "DOLPHINBENCH_HERMES_TOOLSETS": ",".join(TOOLSETS),
            "DOLPHINBENCH_AGENT_IMAGE": self.image,
            # The grader's endpoint is the runner's, never the agent's; the container launcher
            # refuses a loopback address in it even though it does not pass it to the agent.
            "AZURE_OPENAI_ENDPOINT": None,
            **{key: "1" if request.phase == "tests" else "0" for key in READ_ONLY},
        }
        if request.phase == "ingestion":
            values["DOLPHINBENCH_SEED_ITEM_ID"] = f"{request.persona}-{request.interaction_id}"
        # Every model call of the turn goes through the agent relay under this turn's tag; what
        # OpenRouter billed for them is the turn's cost (Hermes cannot price a relayed endpoint).
        tag = (f"{self.home.parent.name}.{request.phase}.{request.persona}.{request.interaction_id}"
               f".a{number:02d}.{uuid.uuid4().hex[:8]}")
        values["AZURE_FOUNDRY_API_KEY"] = f"{os.environ['AZURE_FOUNDRY_API_KEY']}.{tag}"
        cost_path = self.home / "costs" / request.phase / request.persona / f"{request.interaction_id}-{number}.json"
        snapshot = self.profiles / f"{request.persona}.before-turn"
        if request.phase == "ingestion":
            if snapshot.exists():
                raise ValueError(f"An interrupted turn left {snapshot}: put it back as {directory} or remove it")
            clone_tree(directory, snapshot)
        started = time.monotonic()
        with TURN_ENVIRONMENT.overlay(values):
            result = hermes_driver.run_hermes(profile, request.message, timeout=None, model=None,
                                              narrative_time=request.narrative_time)
            if result.session_id or getattr(result, "available_tools", None):
                hermes_driver._validate_hermes_runtime_tools(
                    result, "seed" if request.phase == "ingestion" else "test", request.interaction_id, "builtin")
        duration = (time.monotonic() - started) * 1000
        try:
            save_json(cost_path, {**relay_cost(tag, empty_ok=not result.ok),
                                  "source": "agent relay: OpenRouter's reported cost"})
        except ValueError as exc:
            save_json(cost_path, {"tag": tag, "error": str(exc)})
        if "empty_completion" in str(getattr(result, "error", "") or "") and result.session_id:
            try:
                self._replyless[(request.phase, request.persona, request.interaction_id)] = (
                    tool_results(directory / "state.db", result.session_id), empty_answer_usage(tag))
            except Exception as error:  # the runner then refuses the incomplete record, and the turn runs again
                print(f"{request.persona}/{request.interaction_id}: no tool results for the record: {error}", file=sys.stderr)
        if str(getattr(result, "error", "") or "").startswith("content_policy_blocked"):
            if request.phase == "ingestion":
                shutil.rmtree(directory)
                os.replace(snapshot, directory)
            else:
                shutil.rmtree(directory)
            raise ContentFiltered(str(result.error))
        if request.phase == "ingestion":
            shutil.rmtree(snapshot)
        recording = directory / "submission-traces" / f"{result.session_id}.jsonl"
        return result, recording, duration

    # --- Memory lifecycle ---------------------------------------------------------------------

    def freeze(self, persona):
        past = self.past.freeze(persona)
        return {"hermes": super().freeze(persona), "past": past}

    def verify_checkpoint(self, persona, checkpoint):
        super().verify_checkpoint(persona, checkpoint["hermes"])
        self.past.verify_checkpoint(persona, checkpoint["past"])

    def total_cost_usd(self, phase):
        notes = {"unpriced_calls": 0, "unended_calls": 0}
        if phase == "tests":
            # The tests are over: confirm again that nothing but recall reached any project.
            for persona in PERSONAS:
                checkpoint = load_json((self.home.parent / "checkpoints" / f"{persona}.json").read_bytes())
                self.past.verify_checkpoint(persona, checkpoint["past"])
            notes["failed_recalls"] = sum(path.read_text().count("past.dev recall failed")
                                          for path in (self.home / "test-traces").glob("*/*.jsonl"))
        model = []
        for journal in sorted((self.home / "attempts" / phase).glob("*/*.json")):
            for attempt in load_json(journal.read_bytes()):
                path = self.home / "costs" / phase / journal.parent.name / f"{journal.stem}-{attempt['attempt']}.json"
                if not path.exists():
                    raise ValueError(f"Missing agent cost: {path}; completed interactions must not be replayed")
                record = load_json(path.read_bytes())
                if record.get("error"):
                    record = {**relay_cost(record["tag"], wait_seconds=0, empty_ok=not attempt["driver_ok"],
                                           orphan_after_seconds=1_200),
                              "source": "agent relay: OpenRouter's reported cost"}
                    save_json(path, record)
                notes["unpriced_calls"] += record.get("unpriced_calls", 0)
                notes["unended_calls"] += record.get("unended_calls", 0)
                model.append(check_total_cost(record["total_cost_usd"], str(path)))
        if not model:
            raise ValueError(f"Missing Hermes attempts for {phase}")
        notes["content_filter_reruns"] = 0
        for target in sorted((self.home / "filtered" / phase).glob("*/*")):
            notes["content_filter_reruns"] += 1
            for path in sorted(target.glob("*-*.json")):
                record = load_json(path.read_bytes())
                if record.get("error"):
                    record = {**relay_cost(record["tag"], wait_seconds=0, empty_ok=True, orphan_after_seconds=1_200),
                              "source": "agent relay: OpenRouter's reported cost"}
                    save_json(path, record)
                notes["unpriced_calls"] += record.get("unpriced_calls", 0)
                model.append(check_total_cost(record["total_cost_usd"], str(path)))
        past = self.past.total_cost_usd(phase)  # no model ledger of its own here: the credits alone
        save_json(self.past.state / "cost-notes" / f"{phase}.json",
                  {**notes, "model_usd": math.fsum(model), "past_usd": past})
        if any(notes.values()):
            print(f"{phase} cost notes: {notes}", file=sys.stderr)
        return check_total_cost(round(math.fsum(model) + past, 6), phase)


def relay_cost(tag: str, wait_seconds: float = 120, *, empty_ok: bool = False,
               orphan_after_seconds: float | None = None) -> dict:
    """What OpenRouter billed for one launch's calls, once every call the relay started has ended.

    `empty_ok`: a launch that failed before reaching the relay made no call and costs nothing. A
    start older than `orphan_after_seconds` with no end was left by a relay that was stopped; it is
    counted, not waited on. A call OpenRouter answered without a cost is counted as unpriced."""
    directory = os.environ.get("AGENT_RELAY_TAGS", "").strip()
    if not directory:
        raise ValueError("Set AGENT_RELAY_TAGS, the agent relay's per-turn cost directory")
    path = Path(directory) / f"{tag}.jsonl"
    deadline = time.monotonic() + wait_seconds
    while True:
        records = read_lines(path)
        ended = {record["call"]: record for record in records if record["event"] == "end"}
        open_calls = {record["call"]: record["at"] for record in records
                      if record["event"] == "start" and record["call"] not in ended}
        orphans = {call for call, at in open_calls.items()
                   if orphan_after_seconds is not None and time.time() - at > orphan_after_seconds}
        if set(open_calls) <= orphans:
            break
        if time.monotonic() > deadline:
            raise ValueError(f"{tag}: {len(set(open_calls) - orphans)} relayed call(s) never ended")
        time.sleep(0.5)
    if not ended and not open_calls:
        if empty_ok:
            return {"tag": tag, "calls": 0, "total_cost_usd": 0.0}
        raise ValueError(f"{tag}: the turn made no relayed model call")
    unpriced = [call for call, record in ended.items() if record.get("cost_usd") is None]
    return {"tag": tag, "calls": len(ended), "unpriced_calls": len(unpriced), "unended_calls": len(orphans),
            "total_cost_usd": math.fsum(float(record.get("cost_usd") or 0.0) for record in ended.values())}


def revoke_agent_keys(output: Path) -> None:
    """After the run: the agent's keys are kept for its whole length, so they are revoked by hand."""
    path = output / "past" / "agent-keys.json"
    keys = read_json(path, {})
    harness_ = PastHarness({}, output)
    management = harness_._management_client()
    for persona, key in list(keys.items()):
        try:
            management.revoke_project_key(key["project"], key["id"])
        except Exception as error:  # already revoked: 404
            if getattr(error, "status", None) != 404:
                raise
        print(f"revoked {persona}'s agent key")


if __name__ == "__main__":
    command = sys.argv[1:2]
    if command == ["build-image"]:
        # DolphinBench's image builder, through the Docker context above, then Hermes' lazy
        # dependencies on top of it.
        from reference.execution import build_agent_image
        arguments = sys.argv[2:]
        sys.argv = [sys.argv[0], *arguments]
        build_agent_image.main()
        base = arguments[arguments.index("--tag") + 1] if "--tag" in arguments else "dolphinbench-hermes-agent:local"
        context = os.environ.get("DOLPHINBENCH_DOCKER_CONTEXT") or "default"
        with tempfile.TemporaryDirectory(prefix="db-image-") as raw:
            (Path(raw) / "Dockerfile").write_text(
                f"FROM {base}\nRUN python -m pip install --no-cache-dir {' '.join(LAZY_DEPENDENCIES)}\n")
            subprocess.run(["docker", "--context", context, "build", "--tag", f"{base}-preinstalled", raw], check=True)
        subprocess.run(["docker", "--context", context, "image", "inspect", "--format", "{{.Id}}",
                        f"{base}-preinstalled"], check=True)
    elif command == ["revoke-agent-keys"]:
        revoke_agent_keys(Path(sys.argv[2]))
    else:
        sys.exit("usage: hermes_past.py build-image --source PATH --memory builtin | revoke-agent-keys OUTPUT")
