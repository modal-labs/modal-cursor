"""Bridge a claimed Cursor request to one synchronously provisioned Modal sandbox."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

import httpx
from pydantic import ValidationError

from modal_cursor.pools import (
    APP_NAME_ENV,
    Claim,
    ConfigError,
    Machine,
    build_entrypoint,
    build_worker_env,
)
from modal_cursor.registry import (
    DEFAULT_API_ENDPOINT,
    cursor_client,
    release_claim,
    worker_connected,
)

if TYPE_CHECKING:
    import modal

    from modal_cursor.pool import Pool

SPAWNER_NAME = "spawner"
WORKER_READY_TIMEOUT_S = 120
WORKER_POLL_INTERVAL_S = 1.0


class WorkerProvisioningError(RuntimeError):
    """A sandbox failed to connect its claimed Cursor worker."""


def _wait_for_worker_ready(
    sandbox: modal.Sandbox,
    client: httpx.Client,
    worker_id: str,
    *,
    timeout_s: float = WORKER_READY_TIMEOUT_S,
) -> None:
    """Wait until Cursor sees the worker, failing on early sandbox exit or timeout."""
    deadline = time.monotonic() + timeout_s
    while True:
        returncode = sandbox.poll()
        if returncode is not None:
            raise WorkerProvisioningError(
                f"sandbox {sandbox.object_id} exited with status {returncode} "
                "before worker connected"
            )
        if worker_connected(client, worker_id):
            return
        if time.monotonic() >= deadline:
            sandbox.terminate()
            raise WorkerProvisioningError(
                f"sandbox {sandbox.object_id} did not connect worker within {timeout_s:g}s"
            )
        time.sleep(WORKER_POLL_INTERVAL_S)


def spawn_worker(
    pool: Pool, worker: Machine, app: modal.App, claim_payload: Mapping[str, object]
) -> str:
    """Provision one sandbox and return its ID after its Cursor worker connects."""
    import modal

    claim = Claim.model_validate(claim_payload)
    if claim.pool != pool.name:
        raise ConfigError(f"claim for pool {claim.pool!r} reached spawner for {pool.name!r}")
    api_key = os.environ.get("CURSOR_API_KEY", "")
    worker_env = cast(dict[str, str | None], build_worker_env(worker, claim, api_key))
    sandbox_options = cast(dict[str, Any], dict(worker.sandbox_options))
    sandbox = modal.Sandbox.create(
        *build_entrypoint(pool.name, claim, pool.repo_url),
        image=worker.image,
        secrets=worker.secrets,
        app=app,
        env=worker_env,
        timeout=worker.timeout_s,
        **sandbox_options,
    )
    endpoint = os.environ.get("CURSOR_API_ENDPOINT", pool.api_endpoint)
    with cursor_client(endpoint, api_key) as client:
        _wait_for_worker_ready(sandbox, client, claim.agent_worker_id)
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

    try:
        claim = Claim()
    except ValidationError as error:
        print(f"[spawn] error: invalid claim env: {error}", file=sys.stderr)
        return 2

    app_name = os.environ.get(APP_NAME_ENV)
    api_key = os.environ.get("CURSOR_API_KEY", "")
    if not app_name:
        print(f"[spawn] error: {APP_NAME_ENV} is not set", file=sys.stderr)
        return 2
    if not api_key:
        print("[spawn] error: CURSOR_API_KEY is not set", file=sys.stderr)
        return 2

    try:
        spawner = modal.Function.from_name(app_name, SPAWNER_NAME)
        sandbox_id = spawner.remote(claim.payload())
    except modal.exception.Error as error:
        release_error = _release_failed_claim(claim, api_key)
        suffix = (
            f"; claim release also failed: {release_error}" if release_error else "; claim released"
        )
        print(f"[spawn] error: Modal provisioning failed: {error}{suffix}", file=sys.stderr)
        return 1

    print(
        f"[spawn] pool={claim.pool} worker={claim.agent_worker_id} "
        f"request={claim.request_id} sandbox={sandbox_id}",
        flush=True,
    )
    return 0
