"""Every Past API call the harness makes. Each method is one public endpoint.

A management key (past_mk_...) creates projects and their keys. A project key does the rest:
identities, ingestion, ingestion status and recall.
"""

import asyncio

import httpx

from log import log

# One recall page: at most 100 results, within 8,000 tokens of evidence.
RECALL_LIMIT = 100
RECALL_MAX_TOKENS = 8_000

# A rate limit, a server error or a dropped connection is retried: four attempts in all.
RETRY_DELAYS_SECONDS = (1, 2, 4)
TIMEOUT_SECONDS = 300


class PastApiError(RuntimeError):
    def __init__(self, message: str, *, code: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.code = code  # the API's error code
        self.status = status


class PastApi:
    def __init__(self, base_url: str, key: str) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {key.strip()}", "Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=None),
        )

    async def close(self) -> None:
        await self._http.aclose()

    # --- With the management key --------------------------------------------------------------

    async def create_project(self, name: str) -> str:
        """Creates a project and returns its slug.

        A retried request can find the project its own first attempt created, when that attempt's
        answer was lost, and be refused. Run names are unique, so when creating fails and a project
        with this exact name exists, it is ours: use it. Otherwise the refusal stands.
        """
        try:
            project = await self._call("POST", "/api/v1/projects", {"name": name})
        except PastApiError:
            existing = next((p for p in await self.list_projects() if p["name"] == name), None)
            if existing is None:
                raise
            project = existing
        return project["slug"]

    async def list_projects(self) -> list[dict]:
        """Every live project of the organization: its `name` and `slug`, among other fields."""
        return (await self._call("GET", "/api/v1/projects"))["projects"]

    async def create_project_key(self, slug: str, name: str) -> dict:
        """Creates a key for the project: its `id`, and the `key` itself, shown only in this response."""
        return await self._call("POST", f"/api/v1/projects/{slug}/keys", {"name": name})

    async def revoke_project_key(self, slug: str, key_id: str) -> None:
        await self._call("DELETE", f"/api/v1/projects/{slug}/keys/{key_id}")

    # --- With a project key -------------------------------------------------------------------

    async def register_identities(self, identities: list[dict]) -> None:
        """Registers (or updates) identities: [{"identity": id, "traits": {...}}, ...]."""
        await self._call("POST", "/api/v1/identities/bulk", identities)

    async def ingest_batch(self, items: list[dict], idempotency_key: str) -> dict:
        """Pushes a batch of items. Returns the receipt, whose ingestionId tracks the push."""
        receipt = await self._call("POST", "/api/v1/ingest/batch",
                                   {"idempotencyKey": idempotency_key, "items": items})
        received = [str(item.get("sourceId") or "") for item in receipt.get("items") or []]
        if received != [item["id"] for item in items]:
            raise PastApiError("The ingest receipt does not list the items that were sent, in order")
        return receipt

    async def ingestion_status(self, ingestion_id: str) -> dict:
        """Where a push stands: its `status`, and whether the project has `settled`."""
        return await self._call("GET", f"/api/v1/ingest/{ingestion_id}")

    async def recall(self, query: str, as_of: str, identity: str) -> dict:
        """What the project remembers about `query`, as `identity` knew it at the moment `as_of`."""
        return await self._call("POST", "/api/v1/recall", {
            "query": query,
            "limit": RECALL_LIMIT,
            "maxTokens": RECALL_MAX_TOKENS,
            "queryTimestamp": as_of,
            "identity": identity,
        })

    # --- Transport ----------------------------------------------------------------------------

    async def _call(self, method: str, path: str, body=None) -> dict:
        attempts = len(RETRY_DELAYS_SECONDS) + 1
        for attempt in range(attempts):
            is_last_attempt = attempt == attempts - 1
            try:
                response = await self._http.request(method, path, json=body)
            except httpx.HTTPError as error:
                if is_last_attempt:
                    raise
                log(f"{method} {path} failed ({type(error).__name__}); retrying in {RETRY_DELAYS_SECONDS[attempt]}s")
                await asyncio.sleep(RETRY_DELAYS_SECONDS[attempt])
                continue
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and not is_last_attempt:
                log(f"{method} {path} returned {response.status_code}; retrying in {RETRY_DELAYS_SECONDS[attempt]}s")
                await asyncio.sleep(RETRY_DELAYS_SECONDS[attempt])
                continue
            if response.is_error:
                raise PastApiError(f"{method} {path} returned {response.status_code}: {response.text[:1_000]}",
                                   code=error_code(response), status=response.status_code)
            return response.json() if response.content else {}
        raise AssertionError("unreachable")


def error_code(response: httpx.Response) -> str:
    try:
        return str(response.json().get("code") or "")
    except ValueError:
        return ""
