from __future__ import annotations

from unittest.mock import Mock

from fastapi.testclient import TestClient

from modal_cursor.otel_proxy import create_web_app


def test_otel_proxy_requires_authentication() -> None:
    client = TestClient(create_web_app("secret", otlp_endpoint="https://logfire.test"))

    response = client.post(
        "/v1/logs",
        content=b"payload",
        headers={"Content-Type": "application/x-protobuf"},
    )

    assert response.status_code == 401


def test_otel_proxy_requires_protobuf_content_type() -> None:
    client = TestClient(create_web_app("secret", otlp_endpoint="https://logfire.test"))

    response = client.post("/v1/logs", content=b"payload", headers={"Authorization": "secret"})

    assert response.status_code == 415


def test_otel_proxy_normalizes_success_response(monkeypatch) -> None:
    upstream = Mock(content=b"", status_code=200)

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return upstream

    monkeypatch.setattr("modal_cursor.otel_proxy.httpx.AsyncClient", lambda **kwargs: FakeClient())
    client = TestClient(create_web_app("secret", otlp_endpoint="https://logfire.test"))

    response = client.post(
        "/v1/metrics",
        content=b"payload",
        headers={"X-Logfire-Token": "secret", "Content-Type": "application/x-protobuf"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-protobuf"
