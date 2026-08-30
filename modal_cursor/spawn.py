"""Bridge a claimed Cursor request to one synchronously provisioned Modal sandbox."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Protocol, cast

import httpx
from pydantic import ValidationError

from modal_cursor.pools import (
    APP_NAME_ENV,
    Claim,
    ConfigError,
    Machine,
    RuntimeSettings,
    build_entrypoint,
    build_worker_env,
)
from modal_cursor.registry import (
    DEFAULT_API_ENDPOINT,
    cursor_client,
    release_claim,
    worker_connected,
)
from modal_cursor.telemetry import (
    add_event,
    continue_trace,
    current_span,
    flush_at_exit,
    inject_trace_context,
    instrument,
    record_exception,
    set_attribute,
    span,
)

if TYPE_CHECKING:
    import modal

    from modal_cursor.pool import Pool

SPAWNER_NAME = "spawner"


class _ModalSpawner(Protocol):
    def remote(self, claim_payload: dict[str, object], trace_carrier: dict[str, str]) -> str: ...


class WorkerProvisioningError(RuntimeError):
    """A sandbox failed to connect its claimed Cursor worker."""


@instrument("modal_cursor.worker.wait_for_ready")
def _wait_for_worker_ready(
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
            add_event(
                current,
                "modal_cursor.worker.process_exited",
                {"modal_cursor.worker.poll.attempt": attempt, "process.exit.code": returncode},
            )
            raise WorkerProvisioningError(
                f"sandbox {sandbox.object_id} exited with status {returncode} "
                "before worker connected"
            )
        if not process_alive_recorded:
            process_alive_recorded = True
            add_event(
                current,
                "modal_cursor.worker.sandbox_process_alive",
                {
                    "modal_cursor.worker.poll.attempt": attempt,
                    "modal_cursor.worker.elapsed_ms": round((time.monotonic() - started_at) * 1000),
                },
            )
        with span(
            "modal_cursor.worker.readiness.poll",
            **{"modal_cursor.worker.poll.attempt": attempt},
        ) as poll_span:
            ready = worker_connected(client, worker_id)
            set_attribute(poll_span, "modal_cursor.worker.ready", ready)
            set_attribute(
                poll_span,
                "modal_cursor.worker.poll.outcome",
                "ready" if ready else "not_ready",
            )
        if ready:
            set_attribute(current, "modal_cursor.worker.ready", True)
            set_attribute(current, "modal_cursor.worker.poll.count", attempt)
            add_event(
                current,
                "modal_cursor.worker.registration_succeeded",
                {
                    "modal_cursor.worker.poll.attempt": attempt,
                    "modal_cursor.worker.elapsed_ms": round((time.monotonic() - started_at) * 1000),
                },
            )
            return
        if not registration_pending_recorded:
            registration_pending_recorded = True
            add_event(
                current,
                "modal_cursor.worker.registration_pending",
                {"modal_cursor.worker.poll.attempt": attempt},
            )
        if time.monotonic() >= deadline:
            sandbox.terminate()
            set_attribute(current, "modal_cursor.worker.ready", False)
            set_attribute(current, "modal_cursor.worker.poll.count", attempt)
            add_event(
                current,
                "modal_cursor.worker.registration_timeout",
                {
                    "modal_cursor.worker.poll.attempt": attempt,
                    "modal_cursor.worker.elapsed_ms": round((time.monotonic() - started_at) * 1000),
                },
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
        raise ConfigError(f"claim for pool {claim.pool!r} reached spawner for {pool.name!r}")
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
        _wait_for_worker_ready(sandbox, client, claim.agent_worker_id)
    set_attribute(current, "modal_cursor.sandbox.id", sandbox.object_id)
    print(
        f"[spawn] pool={claim.pool} worker={claim.agent_worker_id} "
        f"request={claim.request_id} sandbox={sandbox.object_id}",
        flush=True,
    )
    return sandbox.object_id


def _release_failed_claim(claim: Claim, api_key: str) -> str | None:
    endpoint = os.environ.get("CURSOR_API_ENDPOINT", DEFAULT_API_ENDPOINT)
    try:
        with cursor_client(endpoint, api_key) as client:
            release_claim(client, claim.request_id)
    except (httpx.HTTPError, ValueError) as error:
        return str(error)
    return None


def main() -> int:
    """Run as Cursor's spawn executable and propagate provisioning failures."""
    import modal

    with (
        flush_at_exit(),
        continue_trace(os.environ),
        span("modal_cursor.controller.dispatch") as current,
    ):
        try:
            claim = Claim()  # pyright: ignore[reportCallIssue]
        except ValidationError as error:
            record_exception(current, error)
            print(f"[spawn] error: invalid claim env: {error}", file=sys.stderr)
            return 2

        set_attribute(current, "modal_cursor.pool.name", claim.pool)
        set_attribute(current, "modal_cursor.worker.id", claim.agent_worker_id)
        set_attribute(current, "modal_cursor.request.id", claim.request_id)
        set_attribute(current, "cursor.conversation.id", claim.request_id)
        set_attribute(current, "modal_cursor.dispatch.phase", "claimed")
        app_name = os.environ.get(APP_NAME_ENV)
        api_key = os.environ.get("CURSOR_API_KEY", "")
        if not app_name:
            set_attribute(current, "modal_cursor.outcome", "configuration_error")
            print(f"[spawn] error: {APP_NAME_ENV} is not set", file=sys.stderr)
            return 2
        if not api_key:
            set_attribute(current, "modal_cursor.outcome", "configuration_error")
            print("[spawn] error: CURSOR_API_KEY is not set", file=sys.stderr)
            return 2

        try:
            from_name = cast(Callable[[str, str], _ModalSpawner], modal.Function.from_name)
            spawner = from_name(app_name, SPAWNER_NAME)
            with span(
                "modal_cursor.modal.spawner.invoke",
                **{
                    "modal_cursor.pool.name": claim.pool,
                    "modal_cursor.worker.id": claim.agent_worker_id,
                    "modal_cursor.request.id": claim.request_id,
                    "cursor.conversation.id": claim.request_id,
                },
            ) as invoke_span:
                trace_carrier: dict[str, str] = {}
                inject_trace_context(trace_carrier)
                add_event(invoke_span, "modal_cursor.modal.remote_call_dispatched")
                remote_started_at = time.monotonic()
                sandbox_id = spawner.remote(claim.payload(), trace_carrier)
                set_attribute(invoke_span, "modal_cursor.sandbox.id", sandbox_id)
                remote_elapsed_ms = round((time.monotonic() - remote_started_at) * 1000)
                set_attribute(
                    invoke_span,
                    "modal_cursor.modal.remote_call.elapsed_ms",
                    remote_elapsed_ms,
                )
                add_event(
                    invoke_span,
                    "modal_cursor.modal.remote_call_completed",
                    {"modal_cursor.modal.remote_call.elapsed_ms": remote_elapsed_ms},
                )
        except modal.exception.Error as error:
            record_exception(current, error)
            release_error = _release_failed_claim(claim, api_key)
            suffix = (
                f"; claim release also failed: {release_error}"
                if release_error
                else "; claim released"
            )
            print(f"[spawn] error: Modal provisioning failed: {error}{suffix}", file=sys.stderr)
            set_attribute(current, "modal_cursor.outcome", "failure")
            return 1

        set_attribute(current, "modal_cursor.sandbox.id", sandbox_id)
        set_attribute(current, "modal_cursor.outcome", "success")
        print(
            f"[spawn] pool={claim.pool} worker={claim.agent_worker_id} "
            f"request={claim.request_id} sandbox={sandbox_id}",
            flush=True,
        )
        return 0
