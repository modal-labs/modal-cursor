"""Typed access to Cursor's worker-pool registry API."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypeAlias, cast
from urllib.parse import quote, urlsplit

import httpx

DEFAULT_API_ENDPOINT = "https://api.cursor.com"
HTTP_NOT_FOUND = 404
REPOSITORY_PATH_PARTS = 2

PoolScope: TypeAlias = Literal["team", "user"]


class RegistrySchemaError(ValueError):
    """Cursor returned a successful response with an invalid pool schema."""


@dataclass(frozen=True, slots=True)
class Repository:
    """GitHub repository identity required by Cursor's pool API."""

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


@dataclass(frozen=True, slots=True)
class PoolRegistration:
    """The complete identity Cursor needs to register one pool."""

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


@dataclass(frozen=True, slots=True)
class RegisteredPool:
    """Pool state returned by Cursor, including live worker counts."""

    name: str
    scope: PoolScope
    connected_workers: int
    in_use_workers: int
    worker_ready_timeout_s: int
    repository: Repository | None = None

    @classmethod
    def from_payload(cls, value: object) -> RegisteredPool:
        if not isinstance(value, Mapping):
            raise RegistrySchemaError("Cursor pool entry is not an object")
        try:
            name = value["poolName"]
            scope = value["scope"]
            connected = value["connectedWorkerCount"]
            in_use = value["inUseWorkerCount"]
            ready_timeout = value["workerReadyTimeoutSeconds"]
        except KeyError as error:
            raise RegistrySchemaError(f"Cursor pool entry is missing {error.args[0]!r}") from error
        if not isinstance(name, str) or scope not in ("team", "user"):
            raise RegistrySchemaError("Cursor pool entry has an invalid name or scope")
        if not all(
            isinstance(item, int) and item >= 0 for item in (connected, in_use, ready_timeout)
        ):
            raise RegistrySchemaError("Cursor pool entry has invalid worker counts or timeout")

        repo_owner = value.get("repoOwner")
        repo_name = value.get("repoName")
        repo_url = value.get("repoUrl")
        repository: Repository | None = None
        if any(item is not None for item in (repo_owner, repo_name, repo_url)):
            if not all(
                isinstance(item, str) and item for item in (repo_owner, repo_name, repo_url)
            ):
                raise RegistrySchemaError(
                    "Cursor repo-backed pool has incomplete repository metadata"
                )
            repository = Repository(
                owner=cast(str, repo_owner),
                name=cast(str, repo_name),
                url=cast(str, repo_url),
            )
        return cls(
            name=name,
            scope=cast(PoolScope, scope),
            connected_workers=connected,
            in_use_workers=in_use,
            worker_ready_timeout_s=ready_timeout,
            repository=repository,
        )


def cursor_client(api_endpoint: str, token: str) -> httpx.Client:
    """Create the one authenticated client used for a Cursor operation."""
    return httpx.Client(base_url=api_endpoint, auth=(token, ""), timeout=30.0)


def register_pool(client: httpx.Client, registration: PoolRegistration) -> None:
    response = client.post("/v0/private-workers/pools", json=registration.request_body())
    response.raise_for_status()


def list_pools(client: httpx.Client) -> list[RegisteredPool]:
    response = client.get("/v0/private-workers/pools")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, Mapping) or not isinstance(payload.get("pools"), list):
        raise RegistrySchemaError("Cursor pool response is missing the 'pools' array")
    return [RegisteredPool.from_payload(item) for item in payload["pools"]]


def deregister_pool(client: httpx.Client, pool: RegisteredPool) -> None:
    params: dict[str, str] = {"scope": pool.scope, "pool_name": pool.name}
    if pool.repository is not None:
        params.update(repo_owner=pool.repository.owner, repo_name=pool.repository.name)
    response = client.delete("/v0/private-workers/pools", params=params)
    response.raise_for_status()


def release_claim(client: httpx.Client, request_id: str) -> None:
    response = client.post(f"/v0/private-workers/claims/{request_id}/release")
    if response.status_code == HTTP_NOT_FOUND:
        return
    response.raise_for_status()


def worker_connected(client: httpx.Client, worker_id: str) -> bool:
    """Return whether Cursor currently exposes the claimed worker identity."""
    response = client.get(f"/v0/private-workers/{quote(worker_id, safe='')}")
    if response.status_code == HTTP_NOT_FOUND:
        return False
    response.raise_for_status()
    payload = response.json()
    worker = payload.get("worker") if isinstance(payload, Mapping) else None
    if not isinstance(worker, Mapping) or worker.get("workerId") != worker_id:
        raise RegistrySchemaError("Cursor worker response has an invalid workerId")
    return True
