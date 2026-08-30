"""Modal application for one Cursor worker pool.

Generated once by modal-cursor. This is application code and may be edited.
"""

import modal
import subprocess

from modal_cursor import Pool
from modal_cursor.spawn import spawn_worker
from modal_cursor.telemetry import span

CURSOR_SECRET_NAME = "cursor-service-account"
WORKER_SECRET_NAMES = ()

pool = Pool(
    name="modal-demo",
    repo_url=None,
    scope="team",
    worker_ready_timeout_s=0,
    api_endpoint="https://api.cursor.com",
)
app = modal.App("modal-cursor-modal-demo", tags={"service": "modal-cursor"})

cursor_secret = modal.Secret.from_name(
    CURSOR_SECRET_NAME,
    required_keys=["CURSOR_API_KEY"],
)
logfire_secret = modal.Secret.from_name("logfire-token", required_keys=["LOGFIRE_TOKEN"])
controller_secrets = [cursor_secret, logfire_secret]
worker_secrets = [modal.Secret.from_name(name) for name in WORKER_SECRET_NAMES]
controller_image = pool.controller_image()

worker_image = (
    pool.worker_image()
    # Add every application-specific build step here.
    # .apt_install("ripgrep")
)

worker = pool.machine(
    image=worker_image,
    secrets=worker_secrets,
    # gpu="A10G",
    # cpu=4,
    # memory=16384,
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
