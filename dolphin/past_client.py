"""Every past.dev call the harness makes. Each method is one public endpoint.

A management key (past_mk_...) creates projects and their keys. A project key does the rest:
identities, ingestion, ingestion status, recall, usage and the request log.
"""

import datetime as dt
import time

import httpx

# One recall page: at most 100 results, within 8,000 tokens of evidence, as on BEAM.
RECALL_LIMIT = 100
RECALL_MAX_TOKENS = 8_000

# A rate limit, a server error or a dropped connection is retried: five attempts in all.
RETRY_DELAYS_SECONDS = (1, 2, 4, 8)
TIMEOUT_SECONDS = 300


class PastApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class PastApi:
    def __init__(self, base_url: str, key: str) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {key.strip()}", "Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
        )

    def close(self) -> None:
        self._http.close()

    # --- With the management key --------------------------------------------------------------

    def create_project(self, name: str) -> str:
        """Creates a project and returns its slug. A retry that finds its own project uses it."""
        try:
            project = self._call("POST", "/api/v1/projects", {"name": name})
        except PastApiError:
            existing = next((p for p in self.list_projects() if p["name"] == name), None)
            if existing is None:
                raise
            project = existing
        return project["slug"]

    def list_projects(self) -> list[dict]:
        return self._call("GET", "/api/v1/projects")["projects"]

    def create_project_key(self, slug: str, name: str) -> dict:
        """A key for the project: its `id`, and the `key` itself, shown only in this response."""
        return self._call("POST", f"/api/v1/projects/{slug}/keys", {"name": name})

    def revoke_project_key(self, slug: str, key_id: str) -> None:
        self._call("DELETE", f"/api/v1/projects/{slug}/keys/{key_id}")

    # --- With a project key -------------------------------------------------------------------

    def register_identities(self, identities: list[dict]) -> None:
        """Registers (or updates) identities: [{"identity": id, "traits": {...}}, ...]."""
        self._call("POST", "/api/v1/identities/bulk", identities)

    def ingest_batch(self, items: list[dict], idempotency_key: str) -> dict:
        """Pushes a batch of items. Returns the receipt, whose ingestionId tracks the push."""
        receipt = self._call("POST", "/api/v1/ingest/batch", {"idempotencyKey": idempotency_key, "items": items})
        received = [str(item.get("sourceId") or "") for item in receipt.get("items") or []]
        if received != [item["id"] for item in items]:
            raise PastApiError("The ingest receipt does not list the items that were sent, in order")
        return receipt

    def ingestion_status(self, ingestion_id: str) -> dict:
        """Where a push stands: its `status`, and whether the project has `settled`."""
        return self._call("GET", f"/api/v1/ingest/{ingestion_id}")

    def explain(self, ingestion_id: str) -> dict:
        """A push's data points, each with whether it is searchable and how many memories it gave."""
        return self._call("GET", f"/api/v1/ingest/{ingestion_id}/explain")

    def recall(self, query: str, as_of: str, identity: str) -> dict:
        """What the project remembers about `query`, as `identity` knew it at the moment `as_of`."""
        return self._call("POST", "/api/v1/recall", {
            "query": query,
            "limit": RECALL_LIMIT,
            "maxTokens": RECALL_MAX_TOKENS,
            "queryTimestamp": as_of,
            "identity": identity,
        })

    def usage(self) -> dict:
        """The billing period's balance and this project's `credits` and `ingestions` in it."""
        return self._call("GET", "/api/v1/usage")

    def requests(self, start: str, end: str) -> list[dict]:
        """The project's request log between two moments: each row's `method`, `path` and the
        `credits` it was billed. The API serves at most 31 days a query, so longer spans are split."""
        rows = []
        window_start = parse_time(start)
        stop = parse_time(end)
        while window_start < stop:
            window_end = min(window_start + dt.timedelta(days=30), stop)
            cursor = None
            while True:
                params = {"from": iso(window_start), "to": iso(window_end), "first": 100}
                if cursor:
                    params["after"] = cursor
                page = self._call("GET", "/api/v1/requests", params=params)
                rows.extend(page.get("requests") or [])
                if not page.get("hasMore"):
                    break
                cursor = page.get("endCursor")
                if not cursor:
                    raise PastApiError("The request log reported more results without a cursor")
            window_start = window_end
        return rows

    # --- Transport ----------------------------------------------------------------------------

    def _call(self, method: str, path: str, body=None, params=None) -> dict:
        attempts = len(RETRY_DELAYS_SECONDS) + 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                response = self._http.request(method, path, json=body, params=params)
            except httpx.HTTPError:
                if last:
                    raise
                time.sleep(RETRY_DELAYS_SECONDS[attempt])
                continue
            if (response.status_code == 429 or response.status_code >= 500) and not last:
                time.sleep(RETRY_DELAYS_SECONDS[attempt])
                continue
            if response.is_error:
                raise PastApiError(f"{method} {path} returned {response.status_code}: {response.text[:1_000]}",
                                   status=response.status_code)
            return response.json() if response.content else {}
        raise AssertionError("unreachable")


def parse_time(value: str) -> dt.datetime:
    moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
