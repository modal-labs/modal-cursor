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
