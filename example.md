# Run Cursor BYOM Pools on Modal

Modal Cursor runs [Cursor Bring Your Own Machine (BYOM) pools](https://cursor.com/docs/cloud-agent/bring-your-own-machine)
in [Modal Sandboxes](https://modal.com/docs/guide/sandboxes). Cursor owns the
Cloud Agent request and pool APIs; Modal Cursor registers pools, claims pending
requests, and creates a sandbox for each claim.

## Before you begin

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- A [Modal account](https://modal.com/docs/guide/modal-user-account-setup); the
  wizard can configure the Modal CLI
- A Cursor service-account API key with access to the worker-pool API

Install `uv` with the
[official standalone installer](https://docs.astral.sh/uv/getting-started/installation/):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

The default configuration uses a Modal Secret named
`cursor-service-account` containing `CURSOR_API_KEY`.

## Deploy a worker pool

Run the interactive setup wizard:

```bash
uvx modal-cursor init
```

The wizard configures Modal if needed, asks for a pool name and Cursor
service-account key, creates the `cursor-service-account` Secret, writes the
pool file, and offers to deploy it. Accept the deployment prompt to start the
control plane.

To review or edit the generated file before deploying, pass a name and
`--no-deploy` instead:

```bash
uvx modal-cursor init gpu-training --no-deploy
```

After editing, deploy all pool files in `pools/`. See Modal's guide to
[managing deployments](https://modal.com/docs/guide/managing-deployments) for
the underlying deployment behavior:

```bash
uvx modal-cursor deploy
```

Verify the deployment:

```bash
uvx modal-cursor doctor
```

## Start a Cloud Agent

In Cursor, start a Cloud Agent using the workflow you normally use. In the
worker or environment selector, choose the `gpu-training` pool before starting
the session.

Modal Cursor claims the session, creates a Modal sandbox, starts the Cursor
worker, and waits for Cursor to report the worker as connected.

## Configure repositories and workers

The generated pool file is ordinary Python. For example, `uvx modal-cursor init
gpu-training` writes:

```python
"""Generated configuration for one editable Cursor worker pool."""

import modal

from modal_cursor import Pool

CURSOR_SECRET_NAME = "cursor-service-account"
WORKER_SECRET_NAMES = ()

pool = Pool(name="gpu-training")
worker = pool.machine(
    image=pool.worker_image(),  # Add application-specific image layers here.
    secrets=[modal.Secret.from_name(name) for name in WORKER_SECRET_NAMES],
    # gpu="A10G",
    # cpu=4,
    # memory=16384,
)
```

Set worker resources in the `pool.machine()` call. The generated file is the
file you edit when configuring the pool. See Modal's guides for [GPU
acceleration](https://modal.com/docs/guide/gpu) and [CPU, memory, and disk
configuration](https://modal.com/docs/guide/resources) for the available
resource options.

### Repository-scoped pools

To make a pool available for one repository, include its HTTPS GitHub URL when
generating the pool:

```bash
uvx modal-cursor init payments \
  --repo-url https://github.com/acme/payments \
  --no-deploy
```

Only URLs in the form `https://github.com/<owner>/<repo>` are accepted. The
repository identity is included in the Cursor pool registration.

For a private repository, add `--private-repo`. The wizard prompts for the
GitHub token and creates the `github-token` Secret:

```bash
uvx modal-cursor init payments \
  --repo-url https://github.com/acme/payments \
  --private-repo \
  --no-deploy
```

`GITHUB_TOKEN` is supplied to a temporary Git credential helper for the clone
and is unset before the Cursor worker starts.

### Custom worker images

`pool.worker_image()` contains the pinned Cursor agent CLI and Git. Extend this
[Modal Image](https://modal.com/docs/guide/images) with tools or application
dependencies before passing it to `pool.machine()`:

```python
worker_image = (
    pool.worker_image()
    .apt_install("ripgrep")
    # .pip_install("your-application-dependency")
)

worker = pool.machine(
    image=worker_image,
    gpu="A10G",
)
```

`pool.machine()` also accepts worker environment values, [Modal
Secrets](https://modal.com/docs/guide/secrets), sandbox timeouts, idle-release
settings, and [Modal Sandbox](https://modal.com/docs/guide/sandboxes) options.
Cursor-managed environment variables and options such as `image`, `secrets`,
and `timeout` cannot be overridden.

Pool names are routing keys used by Cursor. A name must be 1–50 characters,
contain only lowercase letters, digits, and dashes, and start and end with a
letter or digit.

After changing a pool file, deploy again:

```bash
uvx modal-cursor deploy
```

## Remove a deployment

To stop the control plane and deregister one pool:

```bash
uvx modal-cursor destroy pools/gpu-training.py --yes
```

To remove all pools in `pools/`:

```bash
uvx modal-cursor destroy --yes
```

## Reference

### Architecture

The deployment has two parts:

- A single Modal application named `modal-cursor-control-plane` runs the
  controller for all pool files in `pools/`.
- Each claimed request creates one ephemeral Modal sandbox from its pool's
  `Machine` configuration.

The controller consumes Cursor's pending-request stream and routes requests by
the `pool` label. A worker connects to Cursor over an outbound connection, with
no inbound port or public IP address.

### Request lifecycle

For a request assigned to a pool:

1. The controller discovers the pending request from Cursor.
2. It claims the request and obtains the worker identity.
3. It creates a Modal sandbox using the pool's `Machine` configuration.
4. The sandbox clones the requested repository, when applicable, and starts
   the Cursor worker CLI.
5. The controller polls Cursor until the worker is connected.

When the sandbox exits before connecting or the worker remains invisible through
the readiness timeout, provisioning fails and the claim is released for retry.

This integration registers `workerReadyTimeoutSeconds=0`. Workers run in
ephemeral sandboxes; snapshot/restore hibernation and nonzero reconnect windows
remain unavailable.

### Runtime settings

The following environment variables change lifecycle defaults for the
controller and workers:

| Variable | Purpose | Default |
| --- | --- | ---: |
| `MODAL_CURSOR_SANDBOX_TIMEOUT_S` | Maximum sandbox lifetime | `21600` |
| `MODAL_CURSOR_IDLE_RELEASE_TIMEOUT_S` | Idle time before release | `600` |
| `MODAL_CURSOR_SPAWNER_READY_TIMEOUT_S` | Worker registration wait | `120` |
| `MODAL_CURSOR_WORKER_POLL_INTERVAL_S` | Registration polling interval | `1` |
| `MODAL_CURSOR_CONTROLLER_TIMEOUT_S` | Controller invocation lifetime | `86400` |
| `MODAL_CURSOR_CONTROLLER_MAX_RETRIES` | Controller retry count | `10` |

Set `CURSOR_API_ENDPOINT` to use a different Cursor API endpoint. The default
is `https://api.cursor.com`; `modal-cursor init` can also write a custom
endpoint into a pool file.

### Observability

Set `OTEL_EXPORTER_OTLP_ENDPOINT` to export lifecycle and Cursor API spans over
OTLP/HTTP. Instrumentation exports telemetry only when an endpoint is set.
`OTEL_SERVICE_NAME` changes the emitted service name.

The spans include pool, request, worker, sandbox, and outcome metadata. They
omit Cursor API keys, Modal Secret values, and complete claim and machine
payloads.

### Credentials

`CURSOR_API_KEY` is a long-lived service-account key. The controller receives it
from the `CURSOR_SECRET_NAME` Modal Secret and passes it to the Cursor worker
environment. Treat the controller and worker sandboxes as part of this key's
trust boundary.

For private repositories, `GITHUB_TOKEN` is separate from the Cursor key and is
used only during the clone step. It is removed before the worker starts.

For implementation details and development commands, see
[`README.md`](README.md). The external API references are the
[Cursor Cloud Agents API](https://cursor.com/docs/cloud-agent/api/endpoints)
and [Modal documentation](https://modal.com/docs).
