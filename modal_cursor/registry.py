"""Typed access to Cursor's worker-pool registry API."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Literal, TypeAlias, cast
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from modal_cursor.telemetry import current_span, instrument, instrument_httpx, set_attribute

DEFAULT_API_ENDPOINT = "https://api.cursor.com"
HTTP_NOT_FOUND = 404
REPOSITORY_PATH_PARTS = 2

PoolScope: TypeAlias = Literal["team", "user"]


class RegistrySchemaError(ValueError):
    """Cursor returned a successful response with an invalid pool schema."""


class PendingRequest(BaseModel):
    """A request waiting for a worker from one of the registered pools."""

    model_config = ConfigDict(extra="ignore", strict=True)

    request_id: str
    pool: str
    repo_url: str | None = None
    repo_urls: tuple[str, ...] = ()
    claimed_worker_id: str | None = None

    @classmethod
    def from_payload(cls, value: object) -> PendingRequest:  # noqa: PLR0912 - schema validation
        if not isinstance(value, Mapping):
            raise RegistrySchemaError("Cursor pending request is not an object")
        raw = cast(Mapping[str, object], value)
        labels = raw.get("labels")
        pool: str | None = None
        if isinstance(labels, list):
            for label in cast(list[object], labels):
                if not isinstance(label, Mapping):
                    continue
                label_mapping = cast(Mapping[str, object], label)
                if label_mapping.get("key") == "pool" and isinstance(
                    label_mapping.get("value"), str
                ):
                    pool = cast(str, label_mapping["value"])
                    break
        request_id = raw.get("id")
        if not isinstance(request_id, str) or not request_id:
            raise RegistrySchemaError("Cursor pending request is missing an id")
        if not pool:
            raise RegistrySchemaError(f"Cursor pending request {request_id!r} is missing a pool")
        repo_url = raw.get("repoUrl")
        if repo_url is not None and not isinstance(repo_url, str):
            raise RegistrySchemaError(
                f"Cursor pending request {request_id!r} has an invalid repoUrl"
            )
        raw_repo_urls = raw.get("repoUrls")
        repo_urls: tuple[str, ...]
        if raw_repo_urls is None:
            repo_urls = ()
        elif not isinstance(raw_repo_urls, list):
            raise RegistrySchemaError(f"Cursor pending request {request_id!r} has invalid repoUrls")
        else:
            repo_values = cast(list[object], raw_repo_urls)
            if any(not isinstance(url, str) or not url for url in repo_values):
                raise RegistrySchemaError(
                    f"Cursor pending request {request_id!r} has invalid repoUrls"
                )
            repo_urls = tuple(cast(str, url) for url in repo_values)
        claimed_worker_id = raw.get("claimedWorkerId")
        if claimed_worker_id is not None and not isinstance(claimed_worker_id, str):
            raise RegistrySchemaError(
                f"Cursor pending request {request_id!r} has an invalid claimedWorkerId"
            )
        return cls(
            request_id=request_id,
            pool=pool,
            repo_url=repo_url,
            repo_urls=repo_urls,
            claimed_worker_id=claimed_worker_id,
        )

    def claim_payload(self, worker_id: str) -> dict[str, object]:
        """Build the non-secret claim payload consumed by worker provisioning."""
        return {
            "agent_worker_id": worker_id,
            "pool": self.pool,
            "request_id": self.request_id,
            "worker_name": None,
            "repo_url": self.repo_url,
            "repo_urls": self.repo_urls,
        }


class PendingRequestEvent(BaseModel):
    """One event from Cursor's all-pools pending-request stream."""

    model_config = ConfigDict(frozen=True, strict=True)

    event: str
    cursor: str | None = None
    request: PendingRequest | None = None


def _pending_requests_payload(value: object) -> tuple[list[PendingRequest], str, str | None]:
    if not isinstance(value, Mapping):
        raise RegistrySchemaError("Cursor pending-request response is not an object")
    raw = cast(Mapping[str, object], value)
    requests = raw.get("requests")
    cursor = raw.get("streamCursor")
    if not isinstance(requests, list) or not isinstance(cursor, str) or not cursor:
        raise RegistrySchemaError(
            "Cursor pending-request response is missing requests or streamCursor"
        )
    next_page = raw.get("nextPageToken")
    if next_page is not None and (not isinstance(next_page, str) or not next_page):
        raise RegistrySchemaError("Cursor pending-request response has an invalid nextPageToken")
    return (
        [PendingRequest.from_payload(item) for item in cast(list[object], requests)],
        cursor,
        next_page,
    )


class Repository(BaseModel):
    """GitHub repository identity required by Cursor's pool API."""

    model_config = ConfigDict(frozen=True, strict=True)

    owner: str
    name: str
    url: str

    @classmethod
    def from_url(cls, url: str) -> Repository:
        parsed = urlsplit(url)
        parts = [part for part in parsed.path.split("/") if part]
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or len(parts) != REPOSITORY_PATH_PARTS
        ):
            raise ValueError(
                "repo_url must be an HTTPS GitHub repository URL such as "
                "https://github.com/acme/payments"
            )
        owner, name = parts
        name = name.removesuffix(".git")
        if not owner or not name:
            raise ValueError("repo_url must include a repository owner and name")
        return cls(owner=owner, name=name, url=f"https://github.com/{owner}/{name}")


class PoolRegistration(BaseModel):
    """The complete identity Cursor needs to register one pool."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str
    scope: PoolScope
    worker_ready_timeout_s: int = 0
    repository: Repository | None = None

    def request_body(self) -> dict[str, object]:
        body: dict[str, object] = {
            "scope": self.scope,
            "poolName": self.name,
            "workerReadyTimeoutSeconds": self.worker_ready_timeout_s,
        }
        if self.repository is not None:
            body.update(
                repoOwner=self.repository.owner,
                repoName=self.repository.name,
                repoUrl=self.repository.url,
            )
        return body


class RegisteredPool(BaseModel):
    """Pool state returned by Cursor, including live worker counts."""

    model_config = ConfigDict(extra="ignore", strict=True)

    name: str
    scope: PoolScope
    connected_workers: int
    in_use_workers: int
    worker_ready_timeout_s: int
    repository: Repository | None = None

    @model_validator(mode="before")
    @classmethod
    def from_wire_shape(cls, raw: object) -> object:
        if not isinstance(raw, Mapping):
            return raw
        value: dict[str, object] = dict(cast(Mapping[str, object], raw))
        aliases = ("repoOwner", "repoName", "repoUrl")
        repository: tuple[object, ...] = tuple(value.pop(alias, None) for alias in aliases)
        if any(item is not None for item in repository):
            owner, name, url = repository
            if not all(isinstance(item, str) and item for item in repository):
                raise ValueError("Cursor repo-backed pool has incomplete repository metadata")
            if not isinstance(owner, str) or not isinstance(name, str) or not isinstance(url, str):
                raise ValueError("Cursor repo-backed pool has incomplete repository metadata")
            value["repository"] = Repository(owner=owner, name=name, url=url)
        for wire_name, model_name in (
            ("poolName", "name"),
            ("connectedWorkerCount", "connected_workers"),
            ("inUseWorkerCount", "in_use_workers"),
            ("workerReadyTimeoutSeconds", "worker_ready_timeout_s"),
        ):
            if wire_name in value:
                value[model_name] = value.pop(wire_name)
        return value

    @model_validator(mode="after")
    def validate_values(self) -> RegisteredPool:
        if not self.name:
            raise ValueError("Cursor pool entry has an invalid name or scope")
        if any(
            value < 0
            for value in (self.connected_workers, self.in_use_workers, self.worker_ready_timeout_s)
        ):
            raise ValueError("Cursor pool entry has invalid worker counts or timeout")
        return self

    @classmethod
    def from_payload(cls, value: object) -> RegisteredPool:
        if not isinstance(value, Mapping):
            raise RegistrySchemaError("Cursor pool entry is not an object")
        try:
            return cls.model_validate(value)
        except ValidationError as error:
            details = error.errors()[0]
            if details["type"] == "missing":
                location = details["loc"][0]
                if not isinstance(location, str):
                    location = str(location)
                missing = {
                    "name": "poolName",
                    "scope": "scope",
                    "connected_workers": "connectedWorkerCount",
                    "in_use_workers": "inUseWorkerCount",
                    "worker_ready_timeout_s": "workerReadyTimeoutSeconds",
                }.get(location, location)
                raise RegistrySchemaError(f"Cursor pool entry is missing {missing!r}") from error
            if "invalid name or scope" in str(error) or details["loc"][:1] in {
                ("name",),
                ("scope",),
            }:
                raise RegistrySchemaError(
                    "Cursor pool entry has an invalid name or scope"
                ) from error
            if "invalid worker counts or timeout" in str(error) or details["loc"][:1] in {
                ("connected_workers",),
                ("in_use_workers",),
                ("worker_ready_timeout_s",),
            }:
                raise RegistrySchemaError(
                    "Cursor pool entry has invalid worker counts or timeout"
                ) from error
            if "repo-backed" in str(error):
                raise RegistrySchemaError(
                    "Cursor repo-backed pool has incomplete repository metadata"
                ) from error
            raise RegistrySchemaError(f"Cursor pool entry has invalid schema: {error}") from error


def cursor_client(api_endpoint: str, token: str) -> httpx.Client:
    """Create the one authenticated client used for a Cursor operation."""
    client = httpx.Client(base_url=api_endpoint, auth=(token, ""), timeout=30.0)
    instrument_httpx(client)
    return client


@instrument("modal_cursor.registry.register_pool")
def register_pool(client: httpx.Client, registration: PoolRegistration) -> None:
    current = current_span()
    set_attribute(current, "modal_cursor.pool.name", registration.name)
    set_attribute(current, "modal_cursor.pool.scope", registration.scope)
    set_attribute(
        current, "modal_cursor.pool.repository_scoped", registration.repository is not None
    )
    response = client.post("/v0/private-workers/pools", json=registration.request_body())
    set_attribute(current, "http.response.status_code", response.status_code)
    response.raise_for_status()


@instrument("modal_cursor.registry.list_pools")
def list_pools(client: httpx.Client) -> list[RegisteredPool]:
    current = current_span()
    response = client.get("/v0/private-workers/pools")
    set_attribute(current, "http.response.status_code", response.status_code)
    response.raise_for_status()
    payload: object = response.json()
    if not isinstance(payload, Mapping):
        raise RegistrySchemaError("Cursor pool response is missing the 'pools' array")
    pools = cast(Mapping[str, object], payload).get("pools")
    if not isinstance(pools, list):
        raise RegistrySchemaError("Cursor pool response is missing the 'pools' array")
    result = [RegisteredPool.from_payload(item) for item in cast(list[object], pools)]
    set_attribute(current, "modal_cursor.pool.count", len(result))
    return result


@instrument("modal_cursor.registry.list_pending_requests")
def list_pending_requests(client: httpx.Client) -> tuple[list[PendingRequest], str]:
    """List pending requests across every pool visible to this service account."""
    requests: list[PendingRequest] = []
    cursor: str | None = None
    page_token: str | None = None
    while True:
        params = {"pageToken": page_token} if page_token else None
        response = client.get("/v0/private-workers/pending-requests", params=params)
        set_attribute(current_span(), "http.response.status_code", response.status_code)
        response.raise_for_status()
        page_requests, page_cursor, page_token = _pending_requests_payload(response.json())
        if cursor is None:
            cursor = page_cursor
        elif cursor != page_cursor:
            raise RegistrySchemaError("Cursor pending-request pages have different stream cursors")
        requests.extend(page_requests)
        if page_token is None:
            break
    set_attribute(current_span(), "modal_cursor.pending_request.count", len(requests))
    assert cursor is not None
    return requests, cursor


def watch_pending_requests(client: httpx.Client, cursor: str) -> Iterator[PendingRequestEvent]:
    """Yield all-pools pending-request events from Cursor's SSE stream."""
    # The HTTPX instrumentation owns the actual stream span. Do not wrap the
    # generator in another process-lifetime span: the response stays open while
    # the controller waits for events, which delays export and obscures the
    # per-request traces created by the dispatcher.
    with client.stream(
        "GET",
        "/v0/private-workers/pending-requests/stream",
        params={"cursor": cursor},
        headers={"Accept": "text/event-stream"},
        timeout=None,
    ) as response:
        response.raise_for_status()
        set_attribute(current_span(), "http.response.status_code", response.status_code)
        event_name = "message"
        event_cursor: str | None = None
        data_lines: list[str] = []
        for line in response.iter_lines():
            if line == "":
                if data_lines:
                    data = json.loads("\n".join(data_lines))
                    request = (
                        PendingRequest.from_payload(data)
                        if event_name in {"created", "claimed_offline"}
                        else None
                    )
                    yield PendingRequestEvent(
                        event=event_name,
                        cursor=event_cursor,
                        request=request,
                    )
                event_name = "message"
                event_cursor = None
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                event_name = value
            elif field == "id":
                event_cursor = value
            elif field == "data":
                data_lines.append(value)


@instrument("modal_cursor.registry.claim_pending_request")
def claim_pending_request(client: httpx.Client, request_id: str, worker_id: str) -> None:
    """Atomically reserve one pending request for a worker identity."""
    current = current_span()
    set_attribute(current, "modal_cursor.request.id", request_id)
    set_attribute(current, "modal_cursor.worker.id", worker_id)
    response = client.post(
        "/v0/private-workers/claim",
        json={"id": request_id, "workerId": worker_id},
    )
    set_attribute(current, "http.response.status_code", response.status_code)
    response.raise_for_status()


@instrument("modal_cursor.registry.deregister_pool")
def deregister_pool(client: httpx.Client, pool: RegisteredPool) -> None:
    current = current_span()
    set_attribute(current, "modal_cursor.pool.name", pool.name)
    set_attribute(current, "modal_cursor.pool.scope", pool.scope)
    set_attribute(current, "modal_cursor.pool.repository_scoped", pool.repository is not None)
    params: dict[str, str] = {"scope": pool.scope, "pool_name": pool.name}
    if pool.repository is not None:
        params.update(repo_owner=pool.repository.owner, repo_name=pool.repository.name)
    response = client.delete("/v0/private-workers/pools", params=params)
    set_attribute(current, "http.response.status_code", response.status_code)
    response.raise_for_status()


@instrument("modal_cursor.registry.release_claim")
def release_claim(client: httpx.Client, request_id: str) -> None:
    current = current_span()
    set_attribute(current, "modal_cursor.request.id", request_id)
    response = client.post(f"/v0/private-workers/claims/{request_id}/release")
    set_attribute(current, "http.response.status_code", response.status_code)
    if response.status_code == HTTP_NOT_FOUND:
        set_attribute(current, "modal_cursor.claim.already_released", True)
        return
    response.raise_for_status()


def worker_connected(client: httpx.Client, worker_id: str) -> bool:
    """Return whether Cursor currently exposes the claimed worker identity."""
    response = client.get(f"/v0/private-workers/{quote(worker_id, safe='')}")
    if response.status_code == HTTP_NOT_FOUND:
        return False
    response.raise_for_status()
    payload: object = response.json()
    if not isinstance(payload, Mapping):
        raise RegistrySchemaError("Cursor worker response has an invalid workerId")
    payload_mapping = cast(Mapping[str, object], payload)
    worker = payload_mapping.get("worker", payload_mapping)
    if not isinstance(worker, Mapping):
        raise RegistrySchemaError("Cursor worker response has an invalid workerId")
    if cast(Mapping[str, object], worker).get("workerId") != worker_id:
        raise RegistrySchemaError("Cursor worker response has an invalid workerId")
    return True
