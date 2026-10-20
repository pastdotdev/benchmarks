"""Re-extract the pushes the entailment outage left without memories: each push is set to Degraded
in the Pilot database, retried through the API with a temporary project key, and waited for.

    python reextract.py alex reextract-alex.txt [--limit N]
"""
import json
import os
import subprocess
import sys
import time

import httpx

STATE = "/path/to/dolphinbench/tmp/hermes-past-full/past"


def sql(query: str) -> str:
    return subprocess.run(["docker", "exec", "past-pilot-dolphin-postgres", "psql", "-U", "postgres",
                           "-d", "past_org_default", "-Atc", query], check=True, capture_output=True,
                          text=True).stdout.strip()


def main() -> int:
    persona, ids_file = sys.argv[1], sys.argv[2]
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
    ids = [line.strip() for line in open(ids_file) if line.strip()][:limit]
    slug = json.load(open(f"{STATE}/projects.json"))[persona]["slug"]
    url = os.environ["PAST_API_URL"]
    management = httpx.Client(base_url=url, timeout=60,
                              headers={"Authorization": f"Bearer {os.environ['PAST_MANAGEMENT_KEY'].strip()}"})
    created = management.post(f"/api/v1/projects/{slug}/keys", json={"name": "reextract"})
    created.raise_for_status()
    key = created.json()
    client = httpx.Client(base_url=url, timeout=60, headers={"Authorization": f"Bearer {key['key']}"})
    try:
        for push in ids:
            state = sql(f"select processing_state from brain_pushes where id = '{push}'")
            if state == "2":
                sql(f"update brain_pushes set processing_state = 3 where id = '{push}' and processing_state = 2")
            response = client.post(f"/api/v1/ingest/{push}/retry")
            print(persona, push, "retry", response.status_code, flush=True)
            response.raise_for_status()
        deadline = time.monotonic() + 6 * 3600
        while time.monotonic() < deadline:
            quoted = ",".join(f"'{push}'" for push in ids)
            states = sql(f"select processing_state, count(*) from brain_pushes where id in ({quoted}) group by 1")
            print(persona, "states", states.replace("\n", " "), flush=True)
            if states == f"2|{len(ids)}":
                break
            if all(line.split("|")[0] in ("2", "3") for line in states.splitlines()):
                print(persona, "stopped with pushes degraded again", flush=True)
                break
            time.sleep(60)
    finally:
        management.delete(f"/api/v1/projects/{slug}/keys/{key['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
