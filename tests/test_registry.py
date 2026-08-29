from __future__ import annotations

import httpx
import pytest

from modal_cursor.registry import (
    PoolRegistration,
    RegisteredPool,
    RegistrySchemaError,
    Repository,
    deregister_pool,
    list_pools,
    register_pool,
    release_claim,
    worker_connected,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(base_url="https://cursor.test", transport=httpx.MockTransport(handler))


def _pool_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "scope": "team",
        "poolName": "payments",
        "connectedWorkerCount": 2,
        "inUseWorkerCount": 1,
        "workerReadyTimeoutSeconds": 900,
        "repoOwner": "acme",
        "repoName": "payments",
        "repoUrl": "https://github.com/acme/payments",
    }
    payload.update(overrides)
    return payload


def test_register_pool_uses_current_cursor_schema() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"registered": True})

    registration = PoolRegistration(
        name="payments",
        scope="team",
        worker_ready_timeout_s=900,
        repository=Repository.from_url("https://github.com/acme/payments.git"),
    )
    with _client(handler) as client:
        register_pool(client, registration)

    body = __import__("json").loads(requests[0].content)
    assert body["workerReadyTimeoutSeconds"] == 900
    assert "offlineReconnectTimeoutSeconds" not in body
    assert body["repoOwner"] == "acme" and body["repoName"] == "payments"


def test_list_pools_validates_status_and_schema() -> None:
    with (
        _client(lambda request: httpx.Response(500, json={"message": "boom"})) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        list_pools(client)
    with (
        _client(lambda request: httpx.Response(200, json={"unexpected": []})) as client,
        pytest.raises(RegistrySchemaError, match="pools"),
    ):
        list_pools(client)


def test_list_pools_returns_typed_live_state() -> None:
    with _client(lambda request: httpx.Response(200, json={"pools": [_pool_payload()]})) as client:
        [pool] = list_pools(client)
    assert pool.connected_workers == 2
    assert pool.worker_ready_timeout_s == 900
    assert pool.repository == Repository(
        owner="acme", name="payments", url="https://github.com/acme/payments"
    )


def test_deregister_repo_pool_includes_repository_identity() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"deregistered": True})

    pool = RegisteredPool.from_payload(_pool_payload())
    with _client(handler) as client:
        deregister_pool(client, pool)
    params = httpx.QueryParams(requests[0].url.query)
    assert dict(params) == {
        "scope": "team",
        "pool_name": "payments",
        "repo_owner": "acme",
        "repo_name": "payments",
    }


def test_release_claim_treats_missing_claim_as_already_released() -> None:
    with _client(lambda request: httpx.Response(404, json={"error": "missing"})) as client:
        release_claim(client, "bc-1")


def test_worker_connection_requires_matching_live_worker() -> None:
    with _client(lambda request: httpx.Response(404)) as client:
        assert not worker_connected(client, "pw-1")
    with _client(
        lambda request: httpx.Response(200, json={"worker": {"workerId": "pw-1"}})
    ) as client:
        assert worker_connected(client, "pw-1")
    with (
        _client(
            lambda request: httpx.Response(200, json={"worker": {"workerId": "other"}})
        ) as client,
        pytest.raises(RegistrySchemaError, match="workerId"),
    ):
        worker_connected(client, "pw-1")


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:acme/payments.git",
        "https://gitlab.com/acme/payments",
        "https://github.com/acme/payments/issues",
        "https://token@github.com/acme/payments",
    ],
)
def test_repository_rejects_urls_cursor_cannot_register(url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS GitHub"):
        Repository.from_url(url)
