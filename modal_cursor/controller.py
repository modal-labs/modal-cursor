"""All-pools Cursor control loop and Modal worker dispatcher."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING

import httpx

from modal_cursor.pools import Machine
from modal_cursor.registry import (
    PendingRequest,
    claim_pending_request,
    cursor_client,
    list_pending_requests,
    register_pool,
    release_claim,
    watch_pending_requests,
)
from modal_cursor.spawn import spawn_worker
from modal_cursor.telemetry import (
    flush_at_exit,
    inject_trace_context,
    new_root_span,
    record_exception,
    set_attribute,
    span,
)

if TYPE_CHECKING:
    import modal

    from modal_cursor.pool import Pool


@dataclass(frozen=True)
class PoolSpec:
    """The sandbox configuration associated with one Cursor pool."""

    pool: Pool
    worker: Machine


HTTP_GONE = 410


class _Dispatcher:
    def __init__(self, app: modal.App, specs: tuple[PoolSpec, ...], client: httpx.Client) -> None:
        self._app = app
        self._specs = {spec.pool.name: spec for spec in specs}
        self._client = client
        self._executor = ThreadPoolExecutor(max_workers=16)
        self._lock = Lock()
        self._inflight: set[str] = set()

    def submit(self, request: PendingRequest, *, claim_exists: bool = False) -> None:
        with self._lock:
            if request.request_id in self._inflight:
                return
            self._inflight.add(request.request_id)
        # Dispatch runs asynchronously from the Cursor polling/SSE loop. Carry
        # the discovery context as a link rather than a parent so the job trace
        # remains visible and self-contained after the loop continues.
        carrier: dict[str, str] = {}
        inject_trace_context(carrier)
        self._executor.submit(self._dispatch, request, claim_exists, carrier)

    def close(self) -> None:
        self._executor.shutdown(wait=True)

    def _dispatch(
        self,
        request: PendingRequest,
        claim_exists: bool,
        discovery_carrier: Mapping[str, str] | None = None,
    ) -> None:
        claim_acquired = claim_exists
        try:
            spec = self._specs.get(request.pool)
            if spec is None:
                with new_root_span(
                    discovery_carrier or {},
                    "modal_cursor.controller.dispatch",
                    **{
                        "modal_cursor.pool.name": request.pool,
                        "modal_cursor.request.id": request.request_id,
                        "modal_cursor.dispatch.phase": "rejected_unknown_pool",
                        "modal_cursor.outcome": "failure",
                    },
                ):
                    pass
                return
            worker_id = request.claimed_worker_id or f"ctrl-{uuid.uuid4()}"
            claim_payload = request.claim_payload(worker_id)
            with new_root_span(
                discovery_carrier or {},
                "modal_cursor.controller.dispatch",
                **{
                    "modal_cursor.pool.name": request.pool,
                    "modal_cursor.request.id": request.request_id,
                    "cursor.conversation.id": request.request_id,
                    "modal_cursor.worker.id": worker_id,
                    "modal_cursor.dispatch.phase": "discovered",
                },
            ) as current:
                try:
                    if not claim_exists:
                        claim_pending_request(self._client, request.request_id, worker_id)
                        claim_acquired = True
                    set_attribute(current, "modal_cursor.dispatch.phase", "claimed")
                    carrier: dict[str, str] = {}
                    inject_trace_context(carrier)
                    sandbox_id = spawn_worker(
                        spec.pool,
                        spec.worker,
                        self._app,
                        claim_payload,
                        carrier,
                    )
                    set_attribute(current, "modal_cursor.sandbox.id", sandbox_id)
                    set_attribute(current, "modal_cursor.outcome", "success")
                except Exception as error:  # noqa: BLE001 - release every failed claim
                    record_exception(current, error)
                    set_attribute(current, "modal_cursor.outcome", "failure")
                    if claim_acquired:
                        try:
                            release_claim(self._client, request.request_id)
                        except httpx.HTTPError as release_error:
                            record_exception(current, release_error)
                            set_attribute(current, "modal_cursor.claim.release_failed", True)
        finally:
            with self._lock:
                self._inflight.discard(request.request_id)


def _register_pools(client: httpx.Client, specs: tuple[PoolSpec, ...]) -> None:
    with span("modal_cursor.controller.startup") as current:
        set_attribute(current, "modal_cursor.pool.count", len(specs))
        for spec in specs:
            with span(
                "modal_cursor.pool.register",
                **{
                    "modal_cursor.pool.name": spec.pool.name,
                    "modal_cursor.pool.scope": spec.pool.scope,
                    "modal_cursor.pool.repository_scoped": spec.pool.repo_url is not None,
                },
            ):
                register_pool(client, spec.pool.registration)


def run_control_plane(app: modal.App, specs: tuple[PoolSpec, ...]) -> None:
    """Register all pools and dispatch requests from one unfiltered Cursor stream.

    This loop is intentionally not represented by one process-lifetime span.
    Individual dispatches are asynchronous job traces, linked back to the
    controller context at discovery time.
    """
    if not specs:
        raise ValueError("at least one pool is required")
    endpoint = specs[0].pool.api_endpoint
    api_key = os.environ["CURSOR_API_KEY"]
    with flush_at_exit(), cursor_client(endpoint, api_key) as client:
        _register_pools(client, specs)
        dispatcher = _Dispatcher(app, specs, client)
        try:
            while True:
                requests, cursor = list_pending_requests(client)
                for request in requests:
                    dispatcher.submit(request, claim_exists=request.claimed_worker_id is not None)
                try:
                    for event in watch_pending_requests(client, cursor):
                        if event.cursor:
                            cursor = event.cursor
                        if event.request is None:
                            continue
                        if event.event == "created":
                            dispatcher.submit(event.request)
                        elif event.event == "claimed_offline":
                            dispatcher.submit(event.request, claim_exists=True)
                except httpx.HTTPStatusError as error:
                    if error.response.status_code == HTTP_GONE:
                        continue
                    raise
                except httpx.HTTPError:
                    time.sleep(1)
        finally:
            dispatcher.close()
