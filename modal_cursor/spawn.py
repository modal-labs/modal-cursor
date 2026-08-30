"""Provision one synchronously created Modal sandbox for a claimed request."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, cast

import httpx

from modal_cursor.pools import (
    Claim,
    ConfigError,
    Machine,
    RuntimeSettings,
    build_entrypoint,
    build_worker_env,
)
from modal_cursor.registry import (
    cursor_client,
    worker_connected,
)
from modal_cursor.telemetry import (
    continue_trace,
    current_span,
    flush_at_exit,
    instrument,
    set_attribute,
    span,
)

if TYPE_CHECKING:
    import modal

    from modal_cursor.pool import Pool


class WorkerProvisioningError(RuntimeError):
    """A sandbox failed to connect its claimed Cursor worker."""


@instrument("modal_cursor.worker.wait_for_cursor_registration")
def _wait_for_cursor_registration(
    sandbox: modal.Sandbox,
    client: httpx.Client,
    worker_id: str,
    *,
    timeout_s: float | None = None,
) -> None:
    """Wait until Cursor sees the worker, failing on early sandbox exit or timeout."""
    current = current_span()
    set_attribute(current, "modal_cursor.worker.id", worker_id)
    set_attribute(current, "modal_cursor.sandbox.id", sandbox.object_id)
    settings = RuntimeSettings()
    timeout_s = settings.spawner_ready_timeout_s if timeout_s is None else timeout_s
    set_attribute(current, "modal_cursor.worker.ready_timeout_s", timeout_s)
    deadline = time.monotonic() + timeout_s
    started_at = time.monotonic()
    attempt = 0
    process_alive_recorded = False
    registration_pending_recorded = False
    while True:
        attempt += 1
        returncode = sandbox.poll()
        if returncode is not None:
            set_attribute(current, "modal_cursor.worker.process_alive", False)
            set_attribute(current, "modal_cursor.worker.poll.count", attempt)
            set_attribute(current, "modal_cursor.worker.registration_outcome", "process_exited")
            set_attribute(current, "process.exit.code", returncode)
            raise WorkerProvisioningError(
                f"sandbox {sandbox.object_id} exited with status {returncode} "
                "before worker connected"
            )
        if not process_alive_recorded:
            process_alive_recorded = True
            set_attribute(current, "modal_cursor.worker.process_alive", True)
        with span(
            "modal_cursor.worker.registration.poll",
            **{"modal_cursor.worker.poll.attempt": attempt},
        ) as poll_span:
            ready = worker_connected(client, worker_id)
            set_attribute(poll_span, "modal_cursor.worker.ready", ready)
            set_attribute(
                poll_span,
                "modal_cursor.worker.poll.outcome",
                "ready" if ready else "not_ready",
            )
            set_attribute(
                poll_span,
                "modal_cursor.worker.poll.elapsed_ms",
                round((time.monotonic() - started_at) * 1000),
            )
        if ready:
            set_attribute(current, "modal_cursor.worker.ready", True)
            set_attribute(current, "modal_cursor.worker.poll.count", attempt)
            set_attribute(current, "modal_cursor.worker.registration_outcome", "ready")
            set_attribute(
                current,
                "modal_cursor.worker.registration_elapsed_ms",
                round((time.monotonic() - started_at) * 1000),
            )
            return
        if not registration_pending_recorded:
            registration_pending_recorded = True
            set_attribute(current, "modal_cursor.worker.registration_pending", True)
        if time.monotonic() >= deadline:
            sandbox.terminate()
            set_attribute(current, "modal_cursor.worker.ready", False)
            set_attribute(current, "modal_cursor.worker.poll.count", attempt)
            set_attribute(current, "modal_cursor.worker.registration_outcome", "timeout")
            set_attribute(
                current,
                "modal_cursor.worker.registration_elapsed_ms",
                round((time.monotonic() - started_at) * 1000),
            )
            raise WorkerProvisioningError(
                f"sandbox {sandbox.object_id} did not connect worker within {timeout_s:g}s"
            )
        time.sleep(settings.worker_poll_interval_s)


def spawn_worker(
    pool: Pool,
    worker: Machine,
    app: modal.App,
    claim_payload: Mapping[str, object],
    trace_carrier: Mapping[str, str] | None = None,
) -> str:
    """Provision one sandbox and return its ID after its Cursor worker connects."""
    with flush_at_exit(), continue_trace(trace_carrier or {}):
        return _provision_worker(pool, worker, app, claim_payload)


@instrument("modal_cursor.worker.provision")
def _provision_worker(
    pool: Pool, worker: Machine, app: modal.App, claim_payload: Mapping[str, object]
) -> str:
    """Create a Modal sandbox and wait for its Cursor worker to connect."""
    import modal

    current = current_span()
    set_attribute(current, "modal_cursor.pool.name", pool.name)
    claim = Claim.model_validate(claim_payload)
    set_attribute(current, "modal_cursor.worker.id", claim.agent_worker_id)
    set_attribute(current, "modal_cursor.request.id", claim.request_id)
    set_attribute(current, "cursor.conversation.id", claim.request_id)
    if claim.pool != pool.name:
        raise ConfigError(
            f"claim for pool {claim.pool!r} reached worker provisioner for {pool.name!r}"
        )
    api_key = os.environ.get("CURSOR_API_KEY", "")
    worker_env = cast(dict[str, str | None], build_worker_env(worker, claim, api_key))
    sandbox_options: dict[str, object] = dict(worker.sandbox_options)
    create = cast(Callable[..., modal.Sandbox], modal.Sandbox.create)
    with span(
        "modal_cursor.worker.create_sandbox",
        **{
            "modal_cursor.pool.name": pool.name,
            "modal_cursor.worker.id": claim.agent_worker_id,
        },
    ) as create_span:
        sandbox = create(
            *build_entrypoint(pool.name, claim, pool.repo_url),
            image=worker.image,
            secrets=worker.secrets,
            app=app,
            env=worker_env,
            timeout=worker.timeout_s,
            **sandbox_options,
        )
        set_attribute(create_span, "modal_cursor.sandbox.id", sandbox.object_id)
    endpoint = os.environ.get("CURSOR_API_ENDPOINT", pool.api_endpoint)
    with cursor_client(endpoint, api_key) as client:
        _wait_for_cursor_registration(sandbox, client, claim.agent_worker_id)
    set_attribute(current, "modal_cursor.sandbox.id", sandbox.object_id)
    print(
        f"[spawn] pool={claim.pool} worker={claim.agent_worker_id} "
        f"request={claim.request_id} sandbox={sandbox.object_id}",
        flush=True,
    )
    return sandbox.object_id
