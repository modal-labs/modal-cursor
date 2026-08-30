# modal-cursor

Run Cursor bring-your-own-machine worker pools on Modal. Each generated Modal
application owns one durable Cursor pool, one long-running controller, and one
function that provisions an isolated sandbox for each claimed request.

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

`init` writes an editable `pools/gpu-training.py`. Customize its worker image,
resources, secrets, and Modal sandbox options before deploying it.

For a repository-scoped pool:

```bash
uv run modal-cursor init payments \
  --repo-url https://github.com/acme/payments \
  --worker-ready-timeout-s 900
```

Add `--private-repo` to configure a worker-side Modal secret containing
`GITHUB_TOKEN`. Only HTTPS `github.com/<owner>/<repo>` URLs are accepted;
unsupported repository hosts fail during configuration instead of during a
worker launch.

Destroying a pool stops its Modal application and uses the live Cursor registry
record—including `repo_owner` and `repo_name` for repository-scoped pools—to
deregister it:

```bash
uv run modal-cursor destroy pools/payments.py --yes
```

## Runtime design

The runtime has four small boundaries:

- `Pool` owns the canonical pool name, repository scope, Cursor registration,
  and the pinned controller and worker images.
- `Machine` is an immutable worker specification. It rejects environment names
  and Modal options that would override values needed by the bridge.
- `Claim` is a Pydantic Settings model. The local executable bridge validates
  Cursor's claim environment and sends only claim identity and repository data
  to the Modal spawner.
- `registry.py` owns typed request and response models for the Cursor pool and
  claim APIs. Unexpected success payloads fail loudly.

Cursor invokes `/usr/local/bin/modal-cursor-spawn` as an actual executable. The
Modal spawner monitors the sandbox until Cursor exposes the claimed worker ID,
failing on an early process exit or a readiness timeout. The bridge reports
success only after that check; otherwise it releases the Cursor claim and exits
nonzero.

The generated controller registers `workerReadyTimeoutSeconds`, the current
Cursor reconnect-window field. The controller image installs a versioned,
SHA-256-verified Cursor CLI lab-channel archive instead of executing an
unpinned remote install script.

## Credentials

`CURSOR_API_KEY` is a long-lived Cursor service-account key—not a claim-scoped
credential. Store it in a Modal Secret (the generated default is
`cursor-service-account`) and treat every controller and worker sandbox as part
of that credential's trust boundary.

The bridge deliberately excludes this key from the Modal function-call payload.
The controller and spawner receive it from their Modal Secret, and the spawner
injects it directly into the worker environment because the Cursor worker CLI
requires it. Private repository credentials are separate: workers receive
`GITHUB_TOKEN` only when their generated configuration includes the requested
GitHub Modal Secret.

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

The controller startup trace is intentionally separate from each request
trace. The startup context is linked from the request trace, but it is not a
parent because one long-lived controller handles many requests:

```text
Controller startup trace:
modal_cursor.controller.invocation
├─ modal_cursor.pool.register
└─ modal_cursor.controller.run

Per-request trace:
modal_cursor.controller.dispatch       # Cursor claimed request → bridge
└─ modal_cursor.modal.spawner.invoke   # bridge → Modal Function
   └─ modal_cursor.worker.provision
      ├─ modal_cursor.worker.create_sandbox
      └─ modal_cursor.worker.wait_for_cursor_registration
         ├─ modal_cursor.worker.readiness.poll # attempt=1, not_ready
         │  └─ GET 404                       # not visible to Cursor yet
         └─ modal_cursor.worker.readiness.poll # attempt=2, ready
            └─ GET 200                     # worker connected
```

Cursor's controller is a closed-source subprocess, so queue discovery and the
exact claim-selection decision cannot be instrumented from this package. The
`controller.dispatch` span is the reliable boundary: its presence means
Cursor has already claimed a concrete request and invoked the bridge. Each
bridge invocation starts a fresh trace; its downstream Modal spans remain in
that trace via W3C propagation. The controller-startup context is retained as
a span link, never reused as a parent, because the same long-lived controller
process invokes the bridge for many independent requests. The controller is
intentionally started outside the long-lived `process.wait()` call:
OpenTelemetry exports spans when they end, so keeping `controller.invocation`
or `controller.run` open for the controller's entire lifetime would hide the
parent spans and make the trace appear unstructured until shutdown.
Readiness spans record whether the sandbox process remained alive, whether
registration was pending, the poll count, the registration outcome, and the
registration elapsed time. Each readiness poll remains a child span, making
the interval before the spawner function begins visible as remote invocation
startup/scheduling time without turning routine state transitions into extra
records.

## Operations

`modal-cursor doctor` checks more than object existence. It verifies Modal
credentials, declared secrets, a running controller container, the Cursor
registry response schema, registration drift, and connected/in-use worker
counts. Zero connected workers is valid for a scale-to-zero pool; zero running
controller containers is not.

Pool files remain ordinary Python applications. The CLI reads only their
literal secret declarations for diagnostics and derives application identity
from the filename. It does not execute local pool files during lifecycle
commands.

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
separate `cursor-mock` repository mirrors the current
`workerReadyTimeoutSeconds` and repository-aware deregistration contracts for
larger integration tests.

Unit tests mock Modal and Cursor network boundaries. They do not prove that a
new Cursor CLI release can enroll and serve a real agent. Before a production
release, run a disposable live soak test: deploy a pool, create and claim an
agent, observe the worker connect and finish a run, then destroy the pool.

Cursor's API is public beta and may change. Compare releases against the
[Cursor Cloud Agents API](https://cursor.com/docs/cloud-agent/api/endpoints) and
the [Modal documentation](https://modal.com/docs) before upgrading pinned
runtime components.
