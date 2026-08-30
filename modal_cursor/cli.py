"""Command-line lifecycle management for generated Modal pool applications."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import Annotated, Literal, Protocol, cast

import cyclopts
import httpx
import modal
from pydantic import ValidationError
from rich.console import Console
from rich.prompt import Confirm, Prompt

from modal_cursor.pool import Pool
from modal_cursor.pools import ConfigError
from modal_cursor.registry import (
    DEFAULT_API_ENDPOINT,
    PoolScope,
    RegisteredPool,
    RegistrySchemaError,
    cursor_client,
    deregister_pool,
    list_pools,
)
from modal_cursor.telemetry import instrument, record_exception, set_attribute, span

app = cyclopts.App(name="modal-cursor")
TEMPLATE = files("modal_cursor").joinpath("templates", "pool.py.tmpl")
CURSOR_DOCS_URL = "https://cursor.com/docs/account/enterprise/service-accounts"
CONTROL_PLANE_APP_NAME = "modal-cursor-control-plane"
CONTROL_PLANE_FILE = Path(__file__).with_name("control_plane.py")
POOL_FILES_ENV = "MODAL_CURSOR_POOL_FILES"

PoolNameArg = Annotated[
    str, cyclopts.Parameter(help="Lowercase pool slug, for example gpu-training.")
]
RepoUrlOption = Annotated[
    str, cyclopts.Parameter(help="HTTPS GitHub repository URL; omit for any-repo.")
]
ApiEndpointOption = Annotated[str, cyclopts.Parameter(help="Base URL for the Cursor API.")]
ScopeOption = Annotated[Literal["team", "user"], cyclopts.Parameter(help="Cursor pool scope.")]
SecretNameOption = Annotated[
    str, cyclopts.Parameter(help="Modal Secret containing CURSOR_API_KEY.")
]
LogfireSecretNameOption = Annotated[
    str, cyclopts.Parameter(help="Modal Secret containing LOGFIRE_TOKEN.")
]
PoolFileArg = Annotated[
    Path, cyclopts.Parameter(help="Generated pool file; omit to use --pools-dir.")
]
PoolsDirOption = Annotated[
    Path, cyclopts.Parameter(help="Directory containing generated pool files.")
]
YesOption = Annotated[bool, cyclopts.Parameter(name=("--yes", "-y"), negative=False)]

_console = Console(highlight=False, markup=False)


class _ModalFunction(Protocol):
    def spawn(self) -> object: ...

    def get_current_stats(self) -> _ModalStats: ...


class _ModalStats(Protocol):
    num_total_runners: int


class _NamedSecret(Protocol):
    name: str


def _ok(text: str) -> None:
    _console.print(f"✓ {text}", style="green")


def _error(text: str) -> None:
    _console.print(f"✖ {text}", style="red")


def _warn(text: str) -> None:
    _console.print(f"! {text}", style="yellow")


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(question: str, *, password: bool = False) -> str:
    return Prompt.ask(
        question, console=_console, password=password, default="", show_default=False
    ).strip()


def _confirm(question: str, *, default: bool = False) -> bool:
    return bool(Confirm.ask(question, console=_console, default=default))


def _modal(
    *args: str,
    capture_output: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "modal", *args]
    if env is None:
        return subprocess.run(
            command, capture_output=capture_output, text=True, timeout=30, check=False
        )
    return subprocess.run(
        command,
        capture_output=capture_output,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )


def _modal_is_configured() -> bool:
    try:
        return _modal("token", "info").returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _secret_names() -> set[str]:
    """Return configured Modal secret names behind one mockable SDK boundary."""
    secrets = cast(Iterable[_NamedSecret], modal.Secret.objects.list())
    return {secret.name for secret in secrets if secret.name}


def _modal_deploy(pool_files: Path | Iterable[Path]) -> int:
    files = (pool_files,) if isinstance(pool_files, Path) else tuple(pool_files)
    env = os.environ.copy()
    env[POOL_FILES_ENV] = os.pathsep.join(str(file.resolve()) for file in files)
    return _modal(
        "deploy", "--strategy", "rolling", str(CONTROL_PLANE_FILE), capture_output=False, env=env
    ).returncode


def _pool_files(pool_file: Path | None, pools_dir: Path) -> list[Path]:
    candidates = [pool_file] if pool_file is not None else sorted(pools_dir.glob("*.py"))
    if not candidates:
        raise SystemExit(f"No pool files found in {pools_dir}")
    return candidates


def _pool_from_file(file: Path, *, scope: PoolScope = "team") -> Pool:
    try:
        return Pool(name=file.stem, scope=scope)
    except (ConfigError, ValidationError) as error:
        raise SystemExit(
            f"{file} is not a valid pool file name (expected a slug like gpu-training.py)"
        ) from error


def _required_secrets(pool_file: Path) -> set[str]:
    """Read the generated literal secret declarations without executing pool code."""
    try:
        tree = ast.parse(pool_file.read_text(encoding="utf-8"), filename=str(pool_file))
    except (OSError, SyntaxError) as error:
        raise ConfigError(f"cannot inspect {pool_file}: {error}") from error
    values: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in {
            "CURSOR_SECRET_NAME",
            "LOGFIRE_SECRET_NAME",
            "WORKER_SECRET_NAMES",
        }:
            continue
        try:
            values[target.id] = ast.literal_eval(node.value)
        except (TypeError, ValueError, SyntaxError) as error:
            raise ConfigError(f"{pool_file}: {target.id} must be a literal") from error
    cursor_secret = values.get("CURSOR_SECRET_NAME")
    worker_secrets = values.get("WORKER_SECRET_NAMES", ())
    if not isinstance(cursor_secret, str) or not cursor_secret:
        raise ConfigError(f"{pool_file}: CURSOR_SECRET_NAME is missing")
    logfire_secret = values.get("LOGFIRE_SECRET_NAME")
    if logfire_secret is not None and (not isinstance(logfire_secret, str) or not logfire_secret):
        raise ConfigError(f"{pool_file}: LOGFIRE_SECRET_NAME is missing")
    if not isinstance(worker_secrets, (tuple, list)):
        raise ConfigError(f"{pool_file}: WORKER_SECRET_NAMES must be a sequence of names")
    raw_names = tuple(cast(Iterable[object], worker_secrets))
    names = tuple(name for name in raw_names if isinstance(name, str))
    if len(names) != len(raw_names) or not all(names):
        raise ConfigError(f"{pool_file}: WORKER_SECRET_NAMES must be a sequence of names")
    required = {cursor_secret, *names}
    if isinstance(logfire_secret, str):
        required.add(logfire_secret)
    return required


def _deploy_and_start(files: list[Path]) -> bool:
    pools = [_pool_from_file(file) for file in files]
    with span(
        "modal_cursor.cli.deploy_control_plane",
        **{
            "modal_cursor.pool.count": len(pools),
            "modal_cursor.pool.names": ",".join(pool.name for pool in pools),
        },
    ) as current:
        _console.print("Running modal deploy for " + ", ".join(str(file) for file in files) + "...")
        if _modal_deploy(files) != 0:
            set_attribute(current, "modal_cursor.outcome", "failure")
            _error("Control-plane deployment failed")
            return False
        try:
            from_name = cast(Callable[[str, str], _ModalFunction], modal.Function.from_name)
            from_name(CONTROL_PLANE_APP_NAME, "controller").spawn()
        except modal.exception.Error as error:
            record_exception(current, error)
            set_attribute(current, "modal_cursor.outcome", "failure")
            _error(f"Deployed control plane, but the controller failed to start: {error}")
            return False
        _ok(
            "Deployed the all-pools control plane; controller starting for "
            + ", ".join(pool.name for pool in pools)
        )
        set_attribute(current, "modal_cursor.outcome", "success")
        return True


def _stop_control_plane_app() -> bool:
    """Stop the one app that owns registration and dispatch for every pool."""
    with span(
        "modal_cursor.cli.stop_control_plane",
        **{"modal_cursor.app.name": CONTROL_PLANE_APP_NAME},
    ) as current:
        try:
            modal.App.lookup(CONTROL_PLANE_APP_NAME, create_if_missing=False)
        except modal.exception.NotFoundError:
            set_attribute(current, "modal_cursor.outcome", "already_absent")
            _ok(f"Modal app {CONTROL_PLANE_APP_NAME} is not deployed")
            return True
        except modal.exception.Error as error:
            record_exception(current, error)
            set_attribute(current, "modal_cursor.outcome", "failure")
            _error(f"Could not inspect Modal app {CONTROL_PLANE_APP_NAME}: {error}")
            return False
        result = _modal("app", "stop", "--yes", CONTROL_PLANE_APP_NAME)
        if result.returncode != 0:
            set_attribute(current, "modal_cursor.outcome", "failure")
            detail = (result.stderr or result.stdout).strip()
            _error(f"Modal app {CONTROL_PLANE_APP_NAME} stop failed: {detail or result.returncode}")
            return False
        set_attribute(current, "modal_cursor.outcome", "success")
        _ok(f"Stopped Modal app {CONTROL_PLANE_APP_NAME}")
        return True


def _deregister_matches(
    client: httpx.Client,
    pool: Pool,
    scope: PoolScope,
    registry: list[RegisteredPool],
) -> bool:
    matches = [item for item in registry if item.name == pool.name and item.scope == scope]
    if not matches:
        _ok(f"Cursor pool {pool.name} is not registered")
        return True
    succeeded = True
    for registered in matches:
        try:
            deregister_pool(client, registered)
        except httpx.HTTPError as error:
            _error(f"Cursor pool {pool.name} deregistration failed: {error}")
            succeeded = False
            continue
        repo = (
            f" for {registered.repository.owner}/{registered.repository.name}"
            if registered.repository
            else ""
        )
        _ok(f"Deregistered Cursor pool {pool.name}{repo}")
    return succeeded


def _ensure_modal_secret(
    *,
    name: str,
    key: str,
    prompt: str,
    existing: set[str],
    interactive: bool,
    value: str | None = None,
) -> None:
    """Create one explicitly requested secret, or print the exact prerequisite."""
    if name in existing:
        _ok(f"Modal secret {name} exists")
        return
    secret_value = value
    if interactive and not secret_value:
        secret_value = _ask(prompt, password=True)
    if interactive and secret_value and _confirm(f"Save it as Modal secret {name}?", default=True):
        try:
            modal.Secret.objects.create(name, {key: secret_value})
        except modal.exception.Error as error:
            _error(f"Could not create Modal secret {name}: {error}")
        else:
            existing.add(name)
            _ok(f"Created Modal secret {name}")
    if name not in existing:
        _warn(f"Create Modal secret {name} with a {key} value before deploying")


def _check_local_pool(pool_file: Path, pool: Pool, secret_names: set[str]) -> int:
    failures = 0
    missing = sorted(_required_secrets(pool_file) - secret_names)
    if missing:
        _error(f"{pool_file} is missing Modal secret(s): {', '.join(missing)}")
        failures += 1
    else:
        _ok(f"{pool_file} has all required Modal secrets")
    return failures


def _check_control_plane() -> int:
    try:
        modal.App.lookup(CONTROL_PLANE_APP_NAME, create_if_missing=False)
        from_name = cast(Callable[[str, str], _ModalFunction], modal.Function.from_name)
        stats = from_name(CONTROL_PLANE_APP_NAME, "controller").get_current_stats()
    except modal.exception.NotFoundError:
        _error(f"No deployed {CONTROL_PLANE_APP_NAME} controller")
        return 1
    except modal.exception.Error as error:
        _error(f"Could not inspect {CONTROL_PLANE_APP_NAME}: {error}")
        return 1
    if stats.num_total_runners < 1:
        _error(f"{CONTROL_PLANE_APP_NAME} is deployed, but its controller has no running container")
        return 1
    _ok(f"{CONTROL_PLANE_APP_NAME} controller is running")
    return 0


def _check_registry(
    local_pools: list[tuple[Path, Pool]],
    registry: list[RegisteredPool],
    scope: PoolScope,
) -> int:
    failures = 0
    local_identities = {(pool.name, pool.scope, pool.repo_url) for _, pool in local_pools}
    by_name: dict[str, list[RegisteredPool]] = {}
    for item in registry:
        if item.scope == scope:
            by_name.setdefault(item.name, []).append(item)
    for item in registry:
        if item.scope != scope:
            continue
        identity = (item.name, item.scope, item.repository.url if item.repository else None)
        if identity not in local_identities:
            _error(f"Cursor pool {item.name} is registered but has no matching local pool file")
            failures += 1
    for pool_file, pool in local_pools:
        matches = [
            item
            for item in by_name.get(pool.name, [])
            if (item.repository.url if item.repository else None) == pool.repo_url
        ]
        if not matches:
            _error(f"{pool_file} is not registered with matching repository metadata")
            failures += 1
            continue
        if len(matches) > 1:
            _error(f"Cursor pool {pool.name} has duplicate matching registrations")
            failures += 1
            continue
        registered = matches[0]
        if registered.worker_ready_timeout_s != pool.worker_ready_timeout_s:
            _error(
                f"Cursor pool {pool.name} has workerReadyTimeoutSeconds="
                f"{registered.worker_ready_timeout_s}; expected {pool.worker_ready_timeout_s}"
            )
            failures += 1
            continue
        connected = registered.connected_workers
        in_use = registered.in_use_workers
        _ok(f"Cursor pool {pool.name}: {connected} connected, {in_use} in use")
    return failures


@app.command
@instrument("modal_cursor.cli.deploy")
def deploy(
    pool_file: PoolFileArg | None = None,
    *,
    pools_dir: PoolsDirOption = Path("pools"),
) -> None:
    """Deploy one all-pools control plane and start its singleton controller."""
    pool_files = _pool_files(pool_file, pools_dir)
    try:
        for file in pool_files:
            _pool_from_file(file)
    except SystemExit as error:
        raise SystemExit(str(error)) from error
    if not _deploy_and_start(pool_files):
        raise SystemExit("Control-plane deployment failed")


@app.command
@instrument("modal_cursor.cli.destroy")
def destroy(
    pool_file: PoolFileArg | None = None,
    *,
    pools_dir: PoolsDirOption = Path("pools"),
    scope: ScopeOption = "team",
    api_endpoint: ApiEndpointOption = DEFAULT_API_ENDPOINT,
    yes: YesOption = False,
) -> None:
    """Stop the control plane and deregister all matching Cursor pool records."""
    targets = [
        (file, _pool_from_file(file, scope=scope)) for file in _pool_files(pool_file, pools_dir)
    ]
    if not yes:
        if not _interactive():
            raise SystemExit("destroy requires --yes when run non-interactively")
        if not _confirm(
            f"Destroy {len(targets)} pool(s)? This stops their apps and deregisters them."
        ):
            _console.print("Destroy cancelled")
            return

    token = os.environ.get("CURSOR_API_KEY")
    if not token and _interactive():
        token = _ask("Cursor service-account API key", password=True)
    if not token:
        raise SystemExit("CURSOR_API_KEY is required to deregister Cursor pools")

    endpoint = os.environ.get("CURSOR_API_ENDPOINT", api_endpoint)
    try:
        with cursor_client(endpoint, token) as client:
            registry = list_pools(client)
            if not _stop_control_plane_app():
                raise SystemExit("Destroy incomplete; control-plane stop failed")
            failures: list[str] = []
            for file, pool in targets:
                if not _deregister_matches(client, pool, scope, registry):
                    failures.append(f"{file} (Cursor deregistration)")
    except (httpx.HTTPError, RegistrySchemaError, ValueError) as error:
        raise SystemExit(
            f"Could not read the Cursor pool registry; nothing was changed: {error}"
        ) from error

    if failures:
        raise SystemExit("Destroy incomplete; failed: " + ", ".join(failures))


@app.command(name="init")
@instrument("modal_cursor.cli.init")
def init_pool(  # noqa: PLR0912 - one linear CLI workflow with explicit user decisions
    name: PoolNameArg = "",
    *,
    repo_url: RepoUrlOption = "",
    private_repo: Annotated[
        bool, cyclopts.Parameter(help="Configure a GitHub token secret.")
    ] = False,
    github_secret_name: Annotated[
        str,
        cyclopts.Parameter(help="Modal Secret containing GITHUB_TOKEN for a private repository."),
    ] = "github-token",
    scope: ScopeOption = "team",
    api_endpoint: ApiEndpointOption = DEFAULT_API_ENDPOINT,
    secret_name: SecretNameOption = "cursor-service-account",
    logfire_secret_name: LogfireSecretNameOption = "logfire-token",
    pools_dir: PoolsDirOption = Path("pools"),
    deploy: Annotated[
        bool | None, cyclopts.Parameter(help="Deploy after generating the file.")
    ] = None,
) -> None:
    """Generate an editable Modal application for one Cursor worker pool."""
    interactive = _interactive()
    if interactive and not _modal_is_configured() and _confirm("Set up Modal now?", default=True):
        subprocess.run([sys.executable, "-m", "modal", "setup"], check=False)

    while not name:
        if not interactive:
            raise SystemExit("NAME is required")
        name = _ask("Pool name")
    if not secret_name.strip():
        raise SystemExit("secret_name must not be empty")
    if not logfire_secret_name.strip():
        raise SystemExit("logfire_secret_name must not be empty")
    if private_repo and not repo_url:
        raise SystemExit("--private-repo requires --repo-url")
    if private_repo and not github_secret_name.strip():
        raise SystemExit("github_secret_name must not be empty for a private repository")

    try:
        pool = Pool(
            name=name,
            repo_url=repo_url or None,
            scope=scope,
            api_endpoint=api_endpoint,
        )
    except (ConfigError, ValidationError) as error:
        raise SystemExit(str(error)) from error

    out_path = pools_dir / f"{pool.name}.py"
    if out_path.exists():
        raise SystemExit(f"{out_path} already exists, not overwriting")
    worker_secret_names = (github_secret_name,) if private_repo else ()
    pool_options = "".join(
        f", {name}={value!r}"
        for name, value, default in (
            ("repo_url", pool.repo_url, None),
            ("scope", pool.scope, "team"),
            ("api_endpoint", pool.api_endpoint, DEFAULT_API_ENDPOINT),
        )
        if value != default
    )
    generated = Template(TEMPLATE.read_text(encoding="utf-8")).substitute(
        pool_name=repr(pool.name),
        pool_options=pool_options,
        app_name=repr(pool.app_name),
        secret_name=repr(secret_name),
        logfire_secret_name=repr(logfire_secret_name),
        worker_secret_names=repr(worker_secret_names),
    )
    try:
        pools_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(generated, encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"Failed to write {out_path}: {error}") from error
    _ok(f"Wrote {out_path}")

    try:
        existing_secrets: set[str] = _secret_names()
    except modal.exception.Error as error:
        _warn(f"Could not inspect Modal secrets: {error}")
        existing_secrets = set()

    _ensure_modal_secret(
        name=secret_name,
        key="CURSOR_API_KEY",
        prompt=f"Cursor service-account key ({CURSOR_DOCS_URL})",
        existing=existing_secrets,
        interactive=interactive,
        value=os.environ.get("CURSOR_API_KEY"),
    )
    if logfire_secret_name not in existing_secrets:
        _warn(
            f"Modal Secret {logfire_secret_name!r} is missing; create it with a LOGFIRE_TOKEN "
            "before deploying to export telemetry"
        )
    if private_repo:
        _ensure_modal_secret(
            name=github_secret_name,
            key="GITHUB_TOKEN",
            prompt="GitHub token for the private repository",
            existing=existing_secrets,
            interactive=interactive,
        )

    should_deploy = (
        deploy
        if deploy is not None
        else interactive and _confirm(f"Deploy {pool.name} now?", default=True)
    )
    if should_deploy:
        if not _deploy_and_start([out_path]):
            raise SystemExit(1)
    else:
        _console.print(f"Deploy with: modal-cursor deploy {out_path}")


@app.command
@instrument("modal_cursor.cli.doctor")
def doctor(
    *,
    pools_dir: PoolsDirOption = Path("pools"),
    scope: ScopeOption = "team",
    api_endpoint: ApiEndpointOption = DEFAULT_API_ENDPOINT,
) -> None:
    """Verify credentials, secrets, the control-plane runner, and registrations."""
    failures = 0
    if _modal_is_configured():
        _ok("Modal credentials are valid")
    else:
        _error("Modal credentials are missing or invalid; run `modal setup`")
        failures += 1

    try:
        secret_names: set[str] = _secret_names()
    except modal.exception.Error as error:
        _error(f"Could not inspect Modal secrets: {error}")
        secret_names = set()
        failures += 1

    pool_files = sorted(pools_dir.glob("*.py"))
    local_pools: list[tuple[Path, Pool]] = []
    for pool_file in pool_files:
        try:
            pool = _pool_from_file(pool_file, scope=scope)
        except (SystemExit, ConfigError) as error:
            _error(str(error))
            failures += 1
            continue
        local_pools.append((pool_file, pool))
        try:
            failures += _check_local_pool(pool_file, pool, secret_names)
        except ConfigError as error:
            _error(str(error))
            failures += 1

    if local_pools:
        failures += _check_control_plane()

    token = os.environ.get("CURSOR_API_KEY")
    registry: list[RegisteredPool] | None = None
    if token:
        endpoint = os.environ.get("CURSOR_API_ENDPOINT", api_endpoint)
        try:
            with cursor_client(endpoint, token) as client:
                registry = list_pools(client)
        except (httpx.HTTPError, RegistrySchemaError, ValueError) as error:
            _error(f"Could not read Cursor pool registry: {error}")
            failures += 1
    elif local_pools:
        _warn("CURSOR_API_KEY is not set; skipped Cursor registry and worker checks")

    if registry is not None:
        failures += _check_registry(local_pools, registry, scope)

    if failures:
        raise SystemExit(1)


def run() -> None:
    app()
