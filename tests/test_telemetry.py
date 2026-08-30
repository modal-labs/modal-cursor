from __future__ import annotations

import warnings

import httpx
from logfire.testing import CaptureLogfire

from modal_cursor.registry import PoolRegistration, register_pool
from modal_cursor.telemetry import continue_trace, inject_trace_context, new_root_span, span


def test_lifecycle_spans_have_stable_names_and_parenting(capfire: CaptureLogfire) -> None:
    with span("modal_cursor.test.parent", **{"modal_cursor.request.id": "request-1"}):  # noqa: SIM117
        with span("modal_cursor.test.child"):
            pass

    spans = [
        item
        for item in capfire.exporter.exported_spans
        if item.attributes.get("logfire.span_type") == "span"
    ]
    parent = next(item for item in spans if item.name == "modal_cursor.test.parent")
    child = next(item for item in spans if item.name == "modal_cursor.test.child")

    assert child.parent is not None
    assert child.parent.span_id == parent.context.span_id
    assert parent.attributes["modal_cursor.request.id"] == "request-1"


def test_trace_context_survives_subprocess_boundary(capfire: CaptureLogfire) -> None:
    carrier: dict[str, str] = {}
    with span("modal_cursor.test.controller"):
        inject_trace_context(carrier)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        with continue_trace(carrier), span("modal_cursor.test.dispatch"):
            pass

    spans = [
        item
        for item in capfire.exporter.exported_spans
        if item.attributes.get("logfire.span_type") == "span"
    ]
    controller = next(item for item in spans if item.name == "modal_cursor.test.controller")
    dispatch = next(item for item in spans if item.name == "modal_cursor.test.dispatch")

    assert dispatch.parent is not None
    assert dispatch.parent.span_id == controller.context.span_id
    assert dispatch.context.trace_id == controller.context.trace_id


def test_new_root_span_does_not_reuse_controller_trace(capfire: CaptureLogfire) -> None:
    carrier: dict[str, str] = {}
    with span("modal_cursor.test.controller"):
        inject_trace_context(carrier)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        with continue_trace(carrier), new_root_span(carrier, "modal_cursor.test.dispatch"):
            pass

    spans = [
        item
        for item in capfire.exporter.exported_spans
        if item.attributes.get("logfire.span_type") == "span"
    ]
    controller = next(item for item in spans if item.name == "modal_cursor.test.controller")
    dispatch = next(item for item in spans if item.name == "modal_cursor.test.dispatch")

    assert dispatch.parent is None
    assert dispatch.context.trace_id != controller.context.trace_id
    assert len(dispatch.links) == 1
    assert dispatch.links[0].context.trace_id == controller.context.trace_id


def test_registry_spans_keep_payloads_and_credentials_out_of_attributes(
    capfire: CaptureLogfire,
) -> None:
    registration = PoolRegistration(name="payments", scope="team")
    client = httpx.Client(
        base_url="https://cursor.test",
        auth=("service-key", ""),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"registered": True})
        ),
    )
    try:
        register_pool(client, registration)
    finally:
        client.close()

    operation = next(
        item
        for item in capfire.exporter.exported_spans
        if item.name == "modal_cursor.registry.register_pool"
        and item.attributes.get("logfire.span_type") == "span"
    )
    assert operation.attributes["modal_cursor.pool.name"] == "payments"
    assert operation.attributes["http.response.status_code"] == 200
    assert "service-key" not in repr(operation.attributes)
