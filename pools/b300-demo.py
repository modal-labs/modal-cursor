"""Cursor worker pool backed by one Modal B300 GPU per claimed agent."""

import modal

from modal_cursor import Pool
from modal_cursor.spawn import spawn_worker

pool = Pool("b300-demo", scope="team")
app = modal.App("modal-cursor-b300-demo", tags={"service": "modal-cursor"})

cursor_secret = modal.Secret.from_name(
    "cursor-service-account",
    required_keys=["CURSOR_API_KEY"],
)
controller_image = pool.controller_image()
worker = pool.machine(
    image=pool.worker_image(),
    gpu="B300",
)


@app.function(
    image=controller_image,
    secrets=[cursor_secret],
    max_containers=1,
    retries=modal.Retries(max_retries=10),
    timeout=24 * 3600,
)
def controller() -> None:
    pool.register()
    pool.run_controller()


@app.function(image=controller_image, secrets=[cursor_secret])
def spawner(claim_env: dict[str, object]) -> str:
    return spawn_worker(pool, worker, app, claim_env)
