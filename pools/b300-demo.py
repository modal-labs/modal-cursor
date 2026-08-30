"""Cursor worker pool backed by one Modal B300 GPU per claimed agent."""

import modal

from modal_cursor import Pool

pool = Pool(name="b300-demo", scope="team")

CURSOR_SECRET_NAME = "cursor-service-account"
LOGFIRE_SECRET_NAME = "logfire-token"
worker = pool.machine(
    image=pool.worker_image(),
    gpu="B300",
)
