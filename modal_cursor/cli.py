"""Command-line lifecycle management for generated Modal pool applications."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import Annotated, Literal

import cyclopts
import httpx
import modal
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

app = cyclopts.App(name="modal-cursor")
TEMPLATE = files("modal_cursor").joinpath("templates", "pool.py.tmpl")
CURSOR_DOCS_URL = "https://cursor.com/docs/account/enterprise/service-accounts"

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
PoolFileArg = Annotated[
    Path, cyclopts.Parameter(help="Generated pool file; omit to use --pools-dir.")
]
PoolsDirOption = Annotated[
    Path, cyclopts.Parameter(help="Directory containing generated pool files.")
]
YesOption = Annotated[bool, cyclopts.Parameter(name=("--yes", "-y"), negative=False)]

_console = Console(highlight=False, markup=False)


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


def _modal(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "modal", *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _modal_is_configured() -> bool:
    try:
        return _modal("token", "info").returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _secret_names() -> set[str]:
    """Return configured Modal secret names behind one mockable SDK boundary."""
    return {secret.name for secret in modal.Secret.objects.list() if secret.name}


def _modal_deploy(pool_file: Path) -> int:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "modal",
            "deploy",
            "--strategy",
            "rolling",
            str(pool_file),
        ],
        check=False,
    ).returncode


def _pool_files(pool_file: Path | None, pools_dir: Path) -> list[Path]:
    candidates = [pool_file] if pool_file is not None else sorted(pools_dir.glob("*.py"))
    if not candidates:
        raise SystemExit(f"No pool files found in {pools_dir}")
    return candidates


def _pool_from_file(file: Path, *, scope: PoolScope = "team") -> Pool:
    try:
        return Pool(file.stem, scope=scope)
    except ConfigError as error:
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
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name = node.targets[0].id
            if name in {"CURSOR_SECRET_NAME", "WORKER_SECRET_NAMES"}:
                try:
                    values[name] = ast.literal_eval(node.value)
                except (TypeError, ValueError, SyntaxError) as error:
                    raise ConfigError(f"{pool_file}: {name} must be a literal") from error
    cursor_secret = values.get("CURSOR_SECRET_NAME")
    worker_secrets = values.get("WORKER_SECRET_NAMES", ())
    if not isinstance(cursor_secret, str) or not cursor_secret:
        raise ConfigError(f"{pool_file}: CURSOR_SECRET_NAME is missing")
    if not isinstance(worker_secrets, (tuple, list)) or not all(
        isinstance(name, str) and name for name in worker_secrets
    ):
        raise ConfigError(f"{pool_file}: WORKER_SECRET_NAMES must be a sequence of names")
    return {cursor_secret, *worker_secrets}


def _deploy_and_start(file: Path, pool: Pool) -> bool:
    _console.print(f"Running modal deploy {file}...")
    if _modal_deploy(file) != 0:
        _error(f"{file} failed to deploy")
        return False
    try:
        modal.Function.from_name(pool.app_name, "controller").spawn()
    except modal.exception.Error as error:
        _error(f"Deployed {file}, but the controller failed to start: {error}")
        return False
    _ok(f"Deployed {file}; controller starting for pool {pool.name}")
    return True


def _stop_modal_app(pool: Pool) -> bool:
    """Stop one app, treating an already-absent app as success."""
    try:
        modal.App.lookup(pool.app_name, create_if_missing=False)
    except modal.exception.NotFoundError:
        _ok(f"Modal app {pool.app_name} is not deployed")
        return True
    except modal.exception.Error as error:
        _error(f"Could not inspect Modal app {pool.app_name}: {error}")
        return False
    try:
        result = _modal("app", "stop", "--yes", pool.app_name)
    except (OSError, subprocess.TimeoutExpired) as error:
        _error(f"Modal app {pool.app_name} stop failed: {error}")
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        _error(f"Modal app {pool.app_name} stop failed: {detail or result.returncode}")
        return False
    _ok(f"Stopped Modal app {pool.app_name}")
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
    try:
        modal.App.lookup(pool.app_name, create_if_missing=False)
        stats = modal.Function.from_name(pool.app_name, "controller").get_current_stats()
    except modal.exception.NotFoundError:
        _error(f"{pool_file} has no deployed controller")
        return failures + 1
    except modal.exception.Error as error:
        _error(f"Could not inspect controller for {pool_file}: {error}")
        return failures + 1
    if stats.num_total_runners < 1:
        _error(f"{pool_file} is deployed, but its controller has no running container")
        return failures + 1
    _ok(f"{pool_file} controller is running")
    return failures


def _check_registry(
    local_pools: list[tuple[Path, Pool]],
    registry: list[RegisteredPool],
    scope: PoolScope,
) -> int:
    failures = 0
    local_names = {pool.name for _, pool in local_pools}
    registered_names = {item.name for item in registry if item.scope == scope}
    for name in sorted(registered_names - local_names):
        _error(f"Cursor pool {name} is registered but has no local pool file")
        failures += 1
    for pool_file, pool in local_pools:
        matches = [item for item in registry if item.scope == scope and item.name == pool.name]
        if not matches:
            _error(f"{pool_file} is not registered with Cursor")
            failures += 1
            continue
        connected = sum(item.connected_workers for item in matches)
        in_use = sum(item.in_use_workers for item in matches)
        _ok(f"Cursor pool {pool.name}: {connected} connected, {in_use} in use")
    return failures


@app.command
def deploy(
    pool_file: PoolFileArg | None = None,
    *,
    pools_dir: PoolsDirOption = Path("pools"),
) -> None:
    """Deploy pool applications and start their singleton controllers."""
    pool_files = _pool_files(pool_file, pools_dir)
    failures: list[Path] = []
    for file in pool_files:
        try:
            pool = _pool_from_file(file)
        except SystemExit as error:
            _error(str(error))
            failures.append(file)
            continue
        if not _deploy_and_start(file, pool):
            failures.append(file)
    if failures:
        raise SystemExit(
            f"{len(failures)} of {len(pool_files)} pool(s) failed to deploy: "
            + ", ".join(map(str, failures))
        )


@app.command
def destroy(
    pool_file: PoolFileArg | None = None,
    *,
    pools_dir: PoolsDirOption = Path("pools"),
    scope: ScopeOption = "team",
    api_endpoint: ApiEndpointOption = DEFAULT_API_ENDPOINT,
    yes: YesOption = False,
) -> None:
    """Stop Modal applications and deregister all matching Cursor pool records."""
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
            failures: list[str] = []
            for file, pool in targets:
                if not _stop_modal_app(pool):
                    failures.append(f"{file} (Modal stop)")
                    continue
                if not _deregister_matches(client, pool, scope, registry):
                    failures.append(f"{file} (Cursor deregistration)")
    except (httpx.HTTPError, RegistrySchemaError, ValueError) as error:
        raise SystemExit(
            f"Could not read the Cursor pool registry; nothing was changed: {error}"
        ) from error

    if failures:
        raise SystemExit("Destroy incomplete; failed: " + ", ".join(failures))


@app.command(name="init")
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
    worker_ready_timeout_s: Annotated[
        int, cyclopts.Parameter(help="Seconds Cursor waits for an offline worker to reconnect.")
    ] = 0,
    api_endpoint: ApiEndpointOption = DEFAULT_API_ENDPOINT,
    secret_name: SecretNameOption = "cursor-service-account",
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
    if private_repo and not repo_url:
        raise SystemExit("--private-repo requires --repo-url")
    if private_repo and not github_secret_name.strip():
        raise SystemExit("github_secret_name must not be empty for a private repository")

    try:
        pool = Pool(
            name,
            repo_url=repo_url or None,
            scope=scope,
            worker_ready_timeout_s=worker_ready_timeout_s,
            api_endpoint=api_endpoint,
        )
    except ConfigError as error:
        raise SystemExit(str(error)) from error

    out_path = pools_dir / f"{pool.name}.py"
    if out_path.exists():
        raise SystemExit(f"{out_path} already exists, not overwriting")
    worker_secret_names = (github_secret_name,) if private_repo else ()
    generated = Template(TEMPLATE.read_text(encoding="utf-8")).substitute(
        pool_name=repr(pool.name),
        repo_url=repr(pool.repo_url),
        scope=repr(pool.scope),
        worker_ready_timeout_s=repr(pool.worker_ready_timeout_s),
        api_endpoint=repr(pool.api_endpoint),
        app_name=repr(pool.app_name),
        secret_name=repr(secret_name),
        worker_secret_names=repr(worker_secret_names),
    )
    try:
        pools_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(generated, encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"Failed to write {out_path}: {error}") from error
    _ok(f"Wrote {out_path}")

    try:
        existing_secrets = _secret_names()
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
        if not _deploy_and_start(out_path, pool):
            raise SystemExit(1)
    else:
        _console.print(f"Deploy with: modal-cursor deploy {out_path}")


@app.command
def doctor(
    *,
    pools_dir: PoolsDirOption = Path("pools"),
    scope: ScopeOption = "team",
    api_endpoint: ApiEndpointOption = DEFAULT_API_ENDPOINT,
) -> None:
    """Verify credentials, secrets, controller runners, registration, and workers."""
    failures = 0
    if _modal_is_configured():
        _ok("Modal credentials are valid")
    else:
        _error("Modal credentials are missing or invalid; run `modal setup`")
        failures += 1

    try:
        secret_names = _secret_names()
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
