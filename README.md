# modal-cursor

Run [Cursor Cloud Agents](https://cursor.com/docs/cloud-agent) in
[Modal Sandboxes](https://modal.com/docs/guide/sandboxes) with Cursor
Self-Hosted Machines. Select a pool in Cursor, and Modal Cursor starts a Modal
Sandbox for each Cloud Agent session.

## Getting started

You need Python 3.11 or newer, [`uv`](https://docs.astral.sh/uv/), a
[Modal account](https://modal.com/docs/guide/modal-user-account-setup), and a
Cursor service-account API key for pool workers.

Install `uv` if it is not already installed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

From the directory where you want to keep the integration configuration, run:

```bash
uvx modal-cursor init
```

The wizard configures Modal if needed, asks for a pool name and Cursor
service-account key, creates the `cursor-service-account` Modal Secret, writes
`pools/<pool-name>.py`, and offers to deploy the Modal service that registers
and serves the pool.

To review or edit the generated file before deploying:

```bash
export CURSOR_API_KEY="your-service-account-key"
uvx modal-cursor init gpu-training --no-deploy
uvx modal-cursor deploy
uvx modal-cursor doctor
```

Start a Cloud Agent in Cursor and select `gpu-training` in its worker or
machine selector. Modal Cursor claims the request, creates a sandbox, starts
the Cursor worker, and waits for Cursor to report it as connected.

For the complete walkthrough and configuration reference, see
[`example.md`](example.md). The same example is published in the
[Modal documentation](https://modal.com/docs/examples/cursor).

## How it works

- A shared Modal application named `modal-cursor-control-plane` runs the
  controller for all generated pool files.
- The controller watches Cursor's pending requests and routes them by pool.
- Each claimed request creates an ephemeral Modal Sandbox from its pool's
  `Machine` configuration.
- The sandbox clones the requested repository and starts the Cursor worker.

Workers connect outbound to Cursor and do not need an inbound port or public IP
address. Workers are ephemeral; snapshot/restore hibernation and nonzero
`workerReadyTimeoutSeconds` are not supported.

## Observability

Modal Cursor emits OpenTelemetry spans for pool registration, request polling
and claims, sandbox provisioning, worker registration, and outcomes. Set
`OTEL_EXPORTER_OTLP_ENDPOINT` to an OTLP/HTTP endpoint and optionally set
`OTEL_SERVICE_NAME` to customize the service name. Without an export endpoint,
instrumentation is quiet. Spans omit API keys, Modal Secret values, and complete
claim or machine payloads.

## Development

```bash
uv run ruff format --check modal_cursor tests
uv run ruff check modal_cursor tests
uv run mypy
uv run basedpyright
uv run coverage run -m pytest
uv run coverage report
uv build
```

The test suite mocks Modal and Cursor network boundaries. Run a disposable live
soak test before using a new Cursor CLI release in production.

## References

- [Cursor Cloud Agents API](https://cursor.com/docs/cloud-agent/api/endpoints)
- [Cursor Self-Hosted Machines](https://cursor.com/docs/cloud-agent/self-hosted/pool)
- [Modal documentation](https://modal.com/docs)
