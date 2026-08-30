"""Modal application for one Cursor worker pool.

Generated once by modal-cursor. This is application code and may be edited.
"""

import modal

from modal_cursor import Pool

CURSOR_SECRET_NAME = "cursor-service-account"
LOGFIRE_SECRET_NAME = "logfire-token"
WORKER_SECRET_NAMES = ()

pool = Pool(
    name="modal-demo",
    repo_url=None,
    scope="team",
    worker_ready_timeout_s=0,
    api_endpoint="https://api.cursor.com",
)
worker_secrets = [modal.Secret.from_name(name) for name in WORKER_SECRET_NAMES]

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
