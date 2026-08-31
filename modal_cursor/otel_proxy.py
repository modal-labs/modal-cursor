"""Authenticated OTLP/HTTP bridge from Cursor to an OTLP backend."""

from __future__ import annotations

import os
import secrets
from http import HTTPStatus
from typing import Any

import httpx
import modal
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response

APP_NAME = "modal-cursor-otel-bridge"
OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "https://logfire-us.pydantic.dev")
LOGFIRE_SECRET_NAME = os.environ.get("LOGFIRE_SECRET_NAME", "logfire-token")

app = modal.App(APP_NAME)
image = modal.Image.debian_slim(python_version="3.11").pip_install("fastapi", "httpx")
logfire_secret = modal.Secret.from_name(LOGFIRE_SECRET_NAME, required_keys=["LOGFIRE_TOKEN"])


def _authorization_matches(value: str, expected: str) -> bool:
    """Accept the raw Logfire token or the conventional Bearer form."""
    return any(
        secrets.compare_digest(value, candidate) for candidate in (expected, f"Bearer {expected}")
    )


def _content_type_is_protobuf(value: str | None) -> bool:
    return value is not None and value.split(";", 1)[0].strip().lower() == "application/x-protobuf"


def _request_is_authorized(request: Request, expected: str) -> bool:
    """Authenticate with a dedicated header, while accepting Authorization too."""
    dedicated = request.headers.get("x-logfire-token", "")
    authorization = request.headers.get("authorization", "")
    return _authorization_matches(dedicated, expected) or _authorization_matches(
        authorization, expected
    )


def create_web_app(logfire_token: str, *, otlp_endpoint: str = OTLP_ENDPOINT) -> FastAPI:
    """Build the authenticated OTLP bridge application."""
    web_app = FastAPI()

    async def forward(request: Request, signal: str) -> Response:
        if not _request_is_authorized(request, logfire_token):
            return PlainTextResponse("unauthorized", status_code=HTTPStatus.UNAUTHORIZED)
        if not _content_type_is_protobuf(request.headers.get("content-type")):
            return PlainTextResponse(
                "expected application/x-protobuf", status_code=HTTPStatus.UNSUPPORTED_MEDIA_TYPE
            )

        body = await request.body()
        headers = {
            "authorization": logfire_token,
            "content-type": "application/x-protobuf",
            "accept": "application/x-protobuf",
        }
        content_encoding = request.headers.get("content-encoding")
        if content_encoding:
            headers["content-encoding"] = content_encoding
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
                upstream = await client.post(
                    f"{otlp_endpoint}/v1/{signal}", content=body, headers=headers
                )
        except httpx.HTTPError:
            return PlainTextResponse(
                "Logfire upstream unavailable", status_code=HTTPStatus.BAD_GATEWAY
            )

        if not HTTPStatus.OK <= upstream.status_code < HTTPStatus.MULTIPLE_CHOICES:
            return Response(
                content=b"Logfire rejected telemetry",
                status_code=HTTPStatus.BAD_GATEWAY,
                media_type="text/plain",
            )

        # Logfire currently returns an empty 200 response without the OTLP
        # protobuf content type. An empty serialized response message is valid;
        # the explicit media type makes the acknowledgement interoperable with
        # Cursor's strict connection test.
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type="application/x-protobuf",
            headers={"cache-control": "no-store"},
        )

    async def logs(request: Request) -> Response:
        return await forward(request, "logs")

    async def metrics(request: Request) -> Response:
        return await forward(request, "metrics")

    async def healthz() -> Response:
        return Response(content=b"ok", media_type="text/plain")

    web_app.add_api_route("/v1/logs", logs, methods=["POST"])
    web_app.add_api_route("/v1/metrics", metrics, methods=["POST"])
    web_app.add_api_route("/healthz", healthz, methods=["GET"])
    return web_app


@app.function(image=image, secrets=[logfire_secret], timeout=150)  # pyright: ignore[reportUnknownMemberType]
@modal.concurrent(max_inputs=100)  # pyright: ignore[reportUnknownMemberType]
@modal.asgi_app()  # pyright: ignore[reportUnknownMemberType]
def otlp_proxy() -> Any:
    """Expose Cursor's OTLP logs/metrics paths and forward them to Logfire."""
    return create_web_app(os.environ["LOGFIRE_TOKEN"])
