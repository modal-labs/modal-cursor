"""Typed access to Cursor's worker-pool registry API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypeAlias, cast
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

DEFAULT_API_ENDPOINT = "https://api.cursor.com"
HTTP_NOT_FOUND = 404
REPOSITORY_PATH_PARTS = 2

PoolScope: TypeAlias = Literal["team", "user"]


class RegistrySchemaError(ValueError):
    """Cursor returned a successful response with an invalid pool schema."""


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
    return httpx.Client(base_url=api_endpoint, auth=(token, ""), timeout=30.0)


def register_pool(client: httpx.Client, registration: PoolRegistration) -> None:
    response = client.post("/v0/private-workers/pools", json=registration.request_body())
    response.raise_for_status()


def list_pools(client: httpx.Client) -> list[RegisteredPool]:
    response = client.get("/v0/private-workers/pools")
    response.raise_for_status()
    payload: object = response.json()
    if not isinstance(payload, Mapping):
        raise RegistrySchemaError("Cursor pool response is missing the 'pools' array")
    pools = cast(Mapping[str, object], payload).get("pools")
    if not isinstance(pools, list):
        raise RegistrySchemaError("Cursor pool response is missing the 'pools' array")
    return [RegisteredPool.from_payload(item) for item in cast(list[object], pools)]


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
    payload: object = response.json()
    if not isinstance(payload, Mapping):
        raise RegistrySchemaError("Cursor worker response has an invalid workerId")
    worker = cast(Mapping[str, object], payload).get("worker")
    if not isinstance(worker, Mapping):
        raise RegistrySchemaError("Cursor worker response has an invalid workerId")
    if cast(Mapping[str, object], worker).get("workerId") != worker_id:
        raise RegistrySchemaError("Cursor worker response has an invalid workerId")
    return True
