from __future__ import annotations

import httpx
import pytest

from modal_cursor.registry import (
    PendingRequest,
    PoolRegistration,
    RegisteredPool,
    RegistrySchemaError,
    Repository,
    claim_pending_request,
    deregister_pool,
    list_pending_requests,
    list_pools,
    register_pool,
    release_claim,
    watch_pending_requests,
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


def _pending_payload(request_id: str = "bc-1", pool: str = "payments") -> dict[str, object]:
    return {
        "id": request_id,
        "labels": [{"key": "pool", "value": pool}],
        "repoUrl": "https://github.com/acme/payments",
    }


def test_pending_request_routes_by_pool_and_keeps_claim_payload_secret_free() -> None:
    request = PendingRequest.from_payload(_pending_payload())
    assert request.pool == "payments"
    assert request.claim_payload("worker-1") == {
        "agent_worker_id": "worker-1",
        "pool": "payments",
        "request_id": "bc-1",
        "worker_name": None,
        "repo_url": "https://github.com/acme/payments",
        "repo_urls": (),
    }


def test_pending_request_list_is_unfiltered_and_sse_is_cursor_based() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/stream"):
            body = (
                "id: cursor-2\n"
                "event: created\n"
                f"data: {__import__('json').dumps(_pending_payload())}\n\n"
                "id: cursor-3\n"
                "event: heartbeat\n"
                "data: {}\n\n"
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={"requests": [_pending_payload()], "streamCursor": "cursor-1"},
        )

    with _client(handler) as client:
        pending, cursor = list_pending_requests(client)
        events = list(watch_pending_requests(client, cursor))

    assert pending[0].request_id == "bc-1"
    assert cursor == "cursor-1"
    assert events[0].event == "created"
    assert events[0].request is not None
    assert events[1].event == "heartbeat"
    assert events[1].request is None
    assert requests[0].url.params == httpx.QueryParams()
    assert requests[1].url.params["cursor"] == "cursor-1"


def test_claim_pending_request_uses_cursor_claim_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"claimed": True})

    with _client(handler) as client:
        claim_pending_request(client, "bc-1", "worker-1")

    assert __import__("json").loads(requests[0].content) == {
        "id": "bc-1",
        "workerId": "worker-1",
    }


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
    with _client(lambda request: httpx.Response(200, json={"workerId": "pw-1"})) as client:
        assert worker_connected(client, "pw-1")


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
