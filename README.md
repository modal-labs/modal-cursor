# modal-cursor

Run Cursor bring-your-own-machine worker pools on Modal. One durable Modal
control-plane controller owns registration and dispatch for every configured
Cursor pool, then creates an isolated sandbox for each claimed request.

This project targets Python 3.11 and newer.

## Quickstart

```bash
uv sync --all-groups
uv run modal setup
export CURSOR_API_KEY="your-service-account-key"
uv run modal-cursor init gpu-training
uv run modal-cursor deploy
uv run modal-cursor doctor
```

`init` writes an editable `pools/gpu-training.py` configuration. Customize its
worker image, resources, secrets, and Modal sandbox options before deploying
the all-pools control plane.

For a repository-scoped pool:

```bash
uv run modal-cursor init payments \
  --repo-url https://github.com/acme/payments
```

Add `--private-repo` to configure a worker-side Modal secret containing
`GITHUB_TOKEN`. The token is used by a temporary Git credential helper during
clone and is removed before the Cursor worker starts; it is not written into
the repository remote URL. Only HTTPS `github.com/<owner>/<repo>` URLs are
accepted; unsupported repository hosts fail during configuration instead of
during a worker launch.

Destroying a pool stops the shared Modal control plane and uses the live Cursor
registry record—including `repo_owner` and `repo_name` for repository-scoped
pools—to deregister it:

```bash
uv run modal-cursor destroy pools/payments.py --yes
```

## Runtime design

The runtime has four small boundaries:

- `Pool` owns the canonical pool name, repository scope, Cursor registration,
  and the pinned worker/control-plane images.
- `Machine` is an immutable worker specification. It rejects environment names
  and Modal options that would override values needed by the worker.
- `Claim` is a Pydantic Settings model for the non-secret values passed from
  the controller to sandbox provisioning.
- `registry.py` owns typed request and response models for the Cursor pool and
  claim APIs. Unexpected success payloads fail loudly.

The control plane uses Cursor's unfiltered pending-request stream, routes each
request by its pool label, atomically claims it, and provisions the matching
Modal sandbox. The provisioner monitors the sandbox until Cursor exposes the
claimed worker ID, failing on an early sandbox exit or readiness timeout; a
failed claim is released for retry.

This deployment uses ephemeral Modal sandboxes, so it registers
`workerReadyTimeoutSeconds=0`: follow-ups reacquire on a fresh sandbox after a
worker exits. Snapshot/restore hibernation is not supported; nonzero reconnect
windows are rejected during configuration. The controller image installs a
versioned, SHA-256-verified Cursor CLI lab-channel archive instead of
executing an unpinned remote install script.

## Credentials

`CURSOR_API_KEY` is a long-lived Cursor service-account key—not a claim-scoped
credential. Store it in a Modal Secret (the generated default is
`cursor-service-account`) and treat every controller and worker sandbox as part
of that credential's trust boundary.

The controller receives this key from its Modal Secret and injects it directly
into the worker environment because the Cursor worker CLI requires it. Private
repository credentials are separate: the clone shell receives `GITHUB_TOKEN`
only when its generated configuration includes the requested GitHub Modal
Secret, and unsets it before launching the Cursor agent.

Runtime tuning is available through the optional `MODAL_CURSOR_SANDBOX_TIMEOUT_S`,
`MODAL_CURSOR_IDLE_RELEASE_TIMEOUT_S`, `MODAL_CURSOR_SPAWNER_READY_TIMEOUT_S`,
`MODAL_CURSOR_WORKER_POLL_INTERVAL_S`, `MODAL_CURSOR_CONTROLLER_TIMEOUT_S`, and
`MODAL_CURSOR_CONTROLLER_MAX_RETRIES` environment variables.

## Observability

Lifecycle spans and Cursor API request spans are emitted with
[Pydantic Logfire](https://logfire.pydantic.dev/). Set `LOGFIRE_TOKEN` in the
`logfire-token` Modal Secret (or pass another name with
`modal-cursor init --logfire-secret-name`) to send them to Logfire; without a
token, instrumentation is quiet and has no effect on pool operation. The
default service name is `modal-cursor` and can be changed with
`LOGFIRE_SERVICE_NAME`.
Spans include pool, request, worker, sandbox, and outcome metadata, but never
Cursor API keys, Modal Secrets, or complete claim/machine payloads.

Cursor's Enterprise OpenTelemetry Export can be routed through the optional
authenticated Modal bridge when a backend's OTLP acknowledgement is too strict
for Cursor's connection test. Deploy it with:

```console
uv run modal deploy modal_cursor/otel_proxy.py
```

Use the printed `modal.run` URL as Cursor's collector base URL, without `/v1`.
Add an `X-Logfire-Token` header whose value is the Logfire write token stored in
the `logfire-token` Modal Secret, then enable logs and metrics. The bridge
forwards `/v1/logs` and `/v1/metrics` to Logfire and returns a protobuf
acknowledgement. It uses the existing Logfire token for inbound authentication;
deploy behind a separate ingress credential if the endpoint will be shared
beyond this team. `Authorization` is also accepted, but the dedicated header
avoids client-specific authorization-header handling.

The controller does not keep one process-lifetime span open: exporters only
make completed spans queryable, and a durable controller would otherwise hide
its root indefinitely. Registration and pending-request polling are bounded
operational spans. Each asynchronous request dispatch is its own visible root
trace, with a span link back to the controller context at discovery time, so
concurrent requests do not merge into one waterfall:

```text
Control-plane operational spans:
├─ modal_cursor.controller.startup
│  ├─ modal_cursor.pool.register
│  └─ modal_cursor.pool.register
└─ modal_cursor.registry.list_pending_requests

Per-request trace (linked to controller discovery context):
modal_cursor.controller.dispatch
├─ modal_cursor.registry.claim_pending_request
└─ modal_cursor.worker.provision
   ├─ modal_cursor.worker.create_sandbox
   └─ modal_cursor.worker.wait_for_cursor_registration
      ├─ modal_cursor.worker.registration.poll # attempt=1, not_ready
      │  └─ GET 404                       # not visible to Cursor yet
      └─ modal_cursor.worker.registration.poll # attempt=2, ready
         └─ GET 200                     # worker connected
```

Cursor's Enterprise OpenTelemetry export is logs and metrics, not a parent
trace emitted by the Cursor worker controller. The controller therefore owns
the request lifecycle and uses the Cursor request/conversation ID as a
correlation attribute. Cursor's records can be joined in Logfire by
`cursor.conversation.id`, but they cannot be made children of our Modal spans
without a W3C trace context from Cursor. Because discovery and dispatch cross
an asynchronous queue/thread boundary, the controller uses a span link rather
than pretending the dispatch is a synchronous child of the polling loop.
Registration-wait spans record whether the sandbox process remained alive,
whether registration was pending, the poll count, the registration outcome,
and the registration elapsed time. Each registration poll remains a child
span, making the interval before the worker becomes visible to Cursor explicit
without turning routine state transitions into extra records.

## Operations

`modal-cursor doctor` checks more than object existence. It verifies Modal
credentials, declared secrets, the shared control-plane container, the Cursor
registry response schema, registration drift, and connected/in-use worker
counts. Zero connected workers is valid for a scale-to-zero pool; zero running
control-plane containers is not.

Pool files remain ordinary Python configuration modules. The CLI reads only
their literal secret declarations for diagnostics; the deployment module loads
the selected pool files to construct one shared Modal application.

## Development

```bash
uv sync --all-groups
uv run ruff format --check modal_cursor tests
uv run ruff check modal_cursor tests
uv run mypy
uv run basedpyright
uv run coverage run -m pytest
uv run coverage report
uv build
```

The test suite is self-contained; it has no sibling path dependency. The
separate `cursor-mock` repository mirrors the current repository-aware
deregistration contract for larger integration tests.

Unit tests mock Modal and Cursor network boundaries. They do not prove that a
new Cursor CLI release can enroll and serve a real agent. Before a production
release, run a disposable live soak test: deploy a pool, create and claim an
agent, observe the worker connect and finish a run, then destroy the pool.

Cursor's API is public beta and may change. Compare releases against the
[Cursor Cloud Agents API](https://cursor.com/docs/cloud-agent/api/endpoints) and
the [Modal documentation](https://modal.com/docs) before upgrading pinned
runtime components.
