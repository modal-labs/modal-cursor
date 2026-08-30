"""Cursor worker pool backed by one Modal B300 GPU per claimed agent."""

import subprocess

import modal

from modal_cursor import Pool
from modal_cursor.spawn import spawn_worker
from modal_cursor.telemetry import span

pool = Pool(name="b300-demo", scope="team")
app = modal.App("modal-cursor-b300-demo", tags={"service": "modal-cursor"})

CURSOR_SECRET_NAME = "cursor-service-account"
LOGFIRE_SECRET_NAME = "logfire-token"
cursor_secret = modal.Secret.from_name(
    CURSOR_SECRET_NAME,
    required_keys=["CURSOR_API_KEY"],
)
logfire_secret = modal.Secret.from_name(LOGFIRE_SECRET_NAME, required_keys=["LOGFIRE_TOKEN"])
controller_secrets = [cursor_secret, logfire_secret]
controller_image = pool.controller_image()
worker = pool.machine(
    image=pool.worker_image(),
    gpu="B300",
)


@app.function(
    image=controller_image,
    secrets=controller_secrets,
    max_containers=1,
    retries=modal.Retries(max_retries=10),
    timeout=24 * 3600,
)
def controller() -> None:
    with span(
        "modal_cursor.controller.invocation",
        **{"modal_cursor.pool.name": pool.name, "modal_cursor.app.name": pool.app_name},
    ):
        pool.register()
        process = pool.start_controller()
    returncode = process.wait()
    if returncode:
        raise subprocess.CalledProcessError(returncode, process.args)


@app.function(image=controller_image, secrets=controller_secrets)
def spawner(
    claim_env: dict[str, object], trace_carrier: dict[str, str] | None = None
) -> str:
    return spawn_worker(pool, worker, app, claim_env, trace_carrier)
