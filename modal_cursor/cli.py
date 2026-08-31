"""Command-line lifecycle management for generated Modal pool applications."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import Annotated, Literal, Protocol, cast

import cyclopts
import httpx
import modal
from pydantic import ValidationError
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.prompt import Confirm, Prompt
from rich.text import Text

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
CONTROLLER_START_TIMEOUT_S = 30.0
CONTROLLER_START_POLL_INTERVAL_S = 1.0

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

# create-next-app-style prompts make the wizard feel like one coherent flow instead of a
# collection of unrelated library prompts.
_PROMPT_SUFFIX = Text(" › ", style="dim")
_console = Console()


class _WizardPrompt(Prompt):
    prompt_suffix = _PROMPT_SUFFIX  # type: ignore[assignment]


class _WizardConfirm(Confirm):
    prompt_suffix = _PROMPT_SUFFIX  # type: ignore[assignment]


class _ModalFunction(Protocol):
    def spawn(self) -> object: ...

    def get_current_stats(self) -> _ModalStats: ...


class _ModalStats(Protocol):
    num_total_runners: int


class _NamedSecret(Protocol):
    name: str


def _warn(text: str) -> None:
    _console.print(f"! {text}", style="yellow")


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _erase_last_line() -> None:
    if not _interactive():
        return
    _console.file.write("\x1b[1A\x1b[2K")


def _mark_answered(question: str, shown_value: str) -> None:
    """Rewrite a completed prompt so the wizard reads as a finished checklist."""
    _erase_last_line()
    _console.print(
        f"[bold green]✔[/bold green] [bold]{escape(question)}[/bold] [dim]›[/dim] {shown_value}"
    )


def _ask(question: str, *, password: bool = False, default: str = "") -> str:
    answer = str(
        _WizardPrompt.ask(
            f"[bold green]?[/bold green] [bold]{escape(question)}[/bold]",
            console=_console,
            password=password,
            default=default,
            show_default=bool(default),
        )
    ).strip()
    shown = "•" * 8 if password and answer else (escape(answer) or "[dim]skipped[/dim]")
    _mark_answered(question, shown)
    return answer


def _confirm(question: str, *, default: bool = False) -> bool:
    answer = bool(
        _WizardConfirm.ask(
            f"[bold green]?[/bold green] [bold]{escape(question)}[/bold]",
            console=_console,
            default=default,
        )
    )
    _mark_answered(question, "yes" if answer else "no")
    return answer


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


def _step_start(running: str) -> None:
    """Print an in-progress step; interactive mode redraws it on completion."""
    if _interactive():
        _console.print(f"[dim]{escape(running)}[/dim]")
    else:
        _console.print(running)


def _step_done(text: str) -> None:
    if _interactive():
        _erase_last_line()
    _console.print(f"[bold green]✔[/bold green] {text}")


def _step_failed(text: str) -> None:
    if _interactive():
        _erase_last_line()
    _console.print(f"[bold red]✖[/bold red] {text}")


def _step_pending(text: str) -> None:
    if _interactive():
        _erase_last_line()
    _console.print(f"[yellow]![/yellow] {text}")


def _done(text: str) -> None:
    _console.print(f"[bold green]✔[/bold green] {text}")


def _run_with_tail(
    argv: Sequence[str], *, env: Mapping[str, str] | None = None, window: int = 10
) -> int:
    """Run a long-lived command without flooding the terminal with build output."""
    lines: deque[str] = deque(maxlen=window)
    all_lines: list[str] = []
    process = subprocess.Popen(
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=dict(os.environ) if env is None else dict(env),
    )
    if process.stdout is None:  # pragma: no cover - guaranteed by stdout=PIPE
        raise RuntimeError("failed to capture subprocess output")
    try:
        with Live(console=_console, refresh_per_second=12, transient=True) as live:
            for line in process.stdout:
                rendered = line.rstrip()
                lines.append(rendered)
                all_lines.append(rendered)
                live.update(Text("\n".join(lines), style="dim"))
        returncode = process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    visible_lines = all_lines if returncode else list(lines)
    if visible_lines:
        _console.print(Text("\n".join(visible_lines), style="dim"))
    return returncode


def _secret_names() -> set[str]:
    """Return configured Modal secret names behind one mockable SDK boundary."""
    secrets = cast(Iterable[_NamedSecret], modal.Secret.objects.list())
    return {secret.name for secret in secrets if secret.name}


def _modal_deploy(pool_files: Path | Iterable[Path]) -> int:
    files = (pool_files,) if isinstance(pool_files, Path) else tuple(pool_files)
    env = os.environ.copy()
    env[POOL_FILES_ENV] = os.pathsep.join(str(file.resolve()) for file in files)
    argv = [
        sys.executable,
        "-m",
        "modal",
        "deploy",
        "--strategy",
        "recreate",
        str(CONTROL_PLANE_FILE),
    ]
    if _interactive():
        return _run_with_tail(argv, env=env)
    return subprocess.run(argv, env=env, check=False).returncode


def _pool_files(pool_file: Path | None, pools_dir: Path) -> list[Path]:
    candidates = [pool_file] if pool_file is not None else sorted(pools_dir.glob("*.py"))
    if not candidates:
        raise SystemExit(f"No pool files found in {pools_dir}")
    return candidates


def _wait_for_controller(
    function: _ModalFunction,
    *,
    timeout_s: float = CONTROLLER_START_TIMEOUT_S,
    poll_interval_s: float = CONTROLLER_START_POLL_INTERVAL_S,
) -> bool:
    """Wait briefly for Modal to schedule the spawned controller container."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if function.get_current_stats().num_total_runners >= 1:
                return True
        except modal.exception.Error:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_interval_s, remaining))


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
    if not isinstance(worker_secrets, (tuple, list)):
        raise ConfigError(f"{pool_file}: WORKER_SECRET_NAMES must be a sequence of names")
    raw_names = tuple(cast(Iterable[object], worker_secrets))
    names = tuple(name for name in raw_names if isinstance(name, str))
    if len(names) != len(raw_names) or not all(names):
        raise ConfigError(f"{pool_file}: WORKER_SECRET_NAMES must be a sequence of names")
    return {cursor_secret, *names}


def _deploy_and_start(files: list[Path]) -> bool:
    pools = [_pool_from_file(file) for file in files]
    with span(
        "modal_cursor.cli.deploy_control_plane",
        **{
            "modal_cursor.pool.count": len(pools),
            "modal_cursor.pool.names": ",".join(pool.name for pool in pools),
        },
    ) as current:
        _step_start("Deploying the shared Cursor control plane...")
        if _modal_deploy(files) != 0:
            set_attribute(current, "modal_cursor.outcome", "failure")
            _step_failed("Control-plane deployment failed")
            return False
        _step_done("Deployed the shared Cursor control plane")
        _step_start("Starting the control-plane controller...")
        try:
            from_name = cast(Callable[[str, str], _ModalFunction], modal.Function.from_name)
            controller = from_name(CONTROL_PLANE_APP_NAME, "controller")
            controller.spawn()
        except modal.exception.Error as error:
            record_exception(current, error)
            set_attribute(current, "modal_cursor.outcome", "failure")
            _step_failed(f"Controller failed to start: {escape(str(error))}")
            return False
        if _wait_for_controller(controller):
            _step_done(
                "Controller is running for " + ", ".join(escape(pool.name) for pool in pools)
            )
            set_attribute(current, "modal_cursor.outcome", "success")
            return True
        _step_pending(
            "Controller launch submitted, but Modal has not scheduled a running container yet"
        )
        _console.print(
            "[dim]Pool registration will happen when capacity is available. "
            "Check `modal-cursor doctor` or the control-plane logs.[/dim]"
        )
        set_attribute(current, "modal_cursor.outcome", "pending")
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
            _done(f"Modal app {escape(CONTROL_PLANE_APP_NAME)} is not deployed")
            return True
        except modal.exception.Error as error:
            record_exception(current, error)
            set_attribute(current, "modal_cursor.outcome", "failure")
            _step_failed(
                f"Could not inspect Modal app {escape(CONTROL_PLANE_APP_NAME)}: "
                f"{escape(str(error))}"
            )
            return False
        _step_start(f"Stopping Modal app {CONTROL_PLANE_APP_NAME}...")
        try:
            result = _modal("app", "stop", "--yes", CONTROL_PLANE_APP_NAME)
        except (OSError, subprocess.TimeoutExpired) as error:
            _step_failed(f"Modal app stop failed: {escape(str(error))}")
            return False
        if result.returncode != 0:
            set_attribute(current, "modal_cursor.outcome", "failure")
            detail = (result.stderr or result.stdout).strip()
            suffix = f": {escape(detail)}" if detail else f" (exit code {result.returncode})"
            _step_failed(f"Modal app {escape(CONTROL_PLANE_APP_NAME)} stop failed{suffix}")
            return False
        set_attribute(current, "modal_cursor.outcome", "success")
        _step_done(f"Stopped Modal app {escape(CONTROL_PLANE_APP_NAME)}")
        return True


def _deregister_matches(
    client: httpx.Client,
    pool: Pool,
    scope: PoolScope,
    registry: list[RegisteredPool],
) -> bool:
    matches = [item for item in registry if item.name == pool.name and item.scope == scope]
    if not matches:
        _done(f"Cursor pool {escape(pool.name)} is not registered")
        return True
    succeeded = True
    for registered in matches:
        try:
            deregister_pool(client, registered)
        except httpx.HTTPError as error:
            _step_failed(
                f"Cursor pool {escape(pool.name)} deregistration failed: {escape(str(error))}"
            )
            succeeded = False
            continue
        repo = (
            f" for {registered.repository.owner}/{registered.repository.name}"
            if registered.repository
            else ""
        )
        _done(f"Deregistered Cursor pool {escape(pool.name)}{escape(repo)}")
    return succeeded


def _ensure_modal_secret(
    *,
    name: str,
    key: str,
    prompt: str,
    existing: set[str],
    interactive: bool,
    value: str | None = None,
) -> bool:
    """Create one explicitly requested secret, or print the exact prerequisite."""
    if name in existing:
        _done(f"Modal secret [bold]{escape(name)}[/bold] exists")
        return True
    secret_value = value
    if interactive and not secret_value:
        secret_value = _ask(prompt, password=True)
    if interactive and secret_value and _confirm(f"Save it as Modal secret {name}?", default=True):
        _step_start(f"Creating Modal secret {name}...")
        try:
            modal.Secret.objects.create(name, {key: secret_value})
        except modal.exception.Error as error:
            _step_failed(f"Could not create Modal secret {escape(name)}: {escape(str(error))}")
        else:
            existing.add(name)
            _step_done(f"Created Modal secret [bold]{escape(name)}[/bold]")
    if name not in existing:
        _warn(
            f"Create Modal secret {escape(name)} with a [bold]{escape(key)}[/bold] value "
            "before deploying"
        )
        return False
    return True


def _check_local_pool(pool_file: Path, pool: Pool, secret_names: set[str]) -> int:
    failures = 0
    missing = sorted(_required_secrets(pool_file) - secret_names)
    if missing:
        _step_failed(
            f"{escape(str(pool_file))} is missing Modal secret(s): {escape(', '.join(missing))}"
        )
        failures += 1
    else:
        _done(f"{escape(str(pool_file))} has all required Modal secrets")
    return failures


def _check_control_plane() -> int:
    try:
        modal.App.lookup(CONTROL_PLANE_APP_NAME, create_if_missing=False)
        from_name = cast(Callable[[str, str], _ModalFunction], modal.Function.from_name)
        stats = from_name(CONTROL_PLANE_APP_NAME, "controller").get_current_stats()
    except modal.exception.NotFoundError:
        _step_failed(f"No deployed {escape(CONTROL_PLANE_APP_NAME)} controller")
        return 1
    except modal.exception.Error as error:
        _step_failed(f"Could not inspect {escape(CONTROL_PLANE_APP_NAME)}: {escape(str(error))}")
        return 1
    if stats.num_total_runners < 1:
        _step_failed(
            f"{escape(CONTROL_PLANE_APP_NAME)} is deployed, but its controller has no "
            "running container"
        )
        return 1
    _done(f"{escape(CONTROL_PLANE_APP_NAME)} controller is running")
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
            _step_failed(
                f"Cursor pool {escape(item.name)} is registered but has no matching local pool file"
            )
            failures += 1
    for pool_file, pool in local_pools:
        matches = [
            item
            for item in by_name.get(pool.name, [])
            if (item.repository.url if item.repository else None) == pool.repo_url
        ]
        if not matches:
            _step_failed(
                f"{escape(str(pool_file))} is not registered with matching repository metadata"
            )
            failures += 1
            continue
        if len(matches) > 1:
            _step_failed(f"Cursor pool {escape(pool.name)} has duplicate matching registrations")
            failures += 1
            continue
        registered = matches[0]
        if registered.worker_ready_timeout_s != pool.worker_ready_timeout_s:
            _step_failed(
                f"Cursor pool {escape(pool.name)} has workerReadyTimeoutSeconds="
                f"{registered.worker_ready_timeout_s}; expected {pool.worker_ready_timeout_s}"
            )
            failures += 1
            continue
        connected = registered.connected_workers
        in_use = registered.in_use_workers
        _done(f"Cursor pool {escape(pool.name)}: {connected} connected, {in_use} in use")
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
            f"Destroy {len(targets)} pool(s)? This immediately stops the shared Modal "
            "controller and deregisters their Cursor records.",
            default=False,
        ):
            _console.print("[dim]Destroy cancelled[/dim]")
            return

    token = os.environ.get("CURSOR_API_KEY")
    if not token and _interactive():
        token = _ask("Cursor service-account API key", password=True)
    if not token:
        raise SystemExit(
            "CURSOR_API_KEY is required to deregister Cursor pools "
            "(set the env var, or provide it when prompted)"
        )

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
def init_pool(  # noqa: PLR0912, PLR0915 - one linear CLI workflow with explicit user decisions
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
    pools_dir: PoolsDirOption = Path("pools"),
    deploy: Annotated[
        bool | None, cyclopts.Parameter(help="Deploy after generating the file.")
    ] = None,
) -> None:
    """Generate an editable Modal application for one Cursor worker pool."""
    interactive = _interactive()

    if interactive:
        _console.print()
        _console.print("[bold]Let's connect a Cursor worker pool to Modal[/bold]")
        _console.print(
            "[dim]We'll configure credentials, generate the editable pool file, and "
            "help you deploy it.[/dim]"
        )
        _console.print(
            "[dim]Pools serve any repository by default; use --repo-url to pin one "
            "to a specific repository.[/dim]"
        )
        _console.print()

    if interactive and not _modal_is_configured() and _confirm("Set up Modal now?", default=True):
        _step_start("Setting up Modal...")
        subprocess.run([sys.executable, "-m", "modal", "setup"], check=False)
        if _modal_is_configured():
            _step_done("Modal is set up")
        else:
            _step_failed("Modal setup did not complete; run `modal setup` before deploying")

    if interactive and not name:
        _console.print("[dim]Choose a stable pool slug, like gpu-training or production-vpc.[/dim]")
    while not name:
        if not interactive:
            raise SystemExit("NAME is required (pass it as an argument, or run interactively)")
        name = _ask("What would you like to name this pool?")

    if not secret_name.strip():
        raise SystemExit("secret_name must not be empty")
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
        worker_secret_names=repr(worker_secret_names),
    )
    try:
        pools_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(generated, encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"Failed to write {out_path}: {error}") from error
    _done(f"Wrote [cyan]{escape(str(out_path))}[/cyan]")

    try:
        existing_secrets: set[str] = _secret_names()
    except modal.exception.Error as error:
        _warn(f"Could not inspect Modal secrets: {escape(str(error))}")
        existing_secrets = set()

    cursor_secret_ready = _ensure_modal_secret(
        name=secret_name,
        key="CURSOR_API_KEY",
        prompt=f"Cursor service-account key ({CURSOR_DOCS_URL})",
        existing=existing_secrets,
        interactive=interactive,
        value=os.environ.get("CURSOR_API_KEY"),
    )
    if private_repo:
        github_secret_ready = _ensure_modal_secret(
            name=github_secret_name,
            key="GITHUB_TOKEN",
            prompt="GitHub token for the private repository",
            existing=existing_secrets,
            interactive=interactive,
            value=os.environ.get("GITHUB_TOKEN"),
        )
    else:
        github_secret_ready = True

    should_deploy = (
        deploy
        if deploy is not None
        else interactive and _confirm(f"Deploy {pool.name} now?", default=True)
    )
    if should_deploy:
        if not cursor_secret_ready or not github_secret_ready:
            raise SystemExit(
                "Deployment prerequisites are missing; create the listed Modal secret(s) first"
            )
        if not _deploy_and_start([out_path]):
            raise SystemExit(1)
    else:
        _console.print("[dim]When you're ready, deploy with:[/dim]")
        _console.print(f"  modal-cursor deploy [cyan]{escape(str(out_path))}[/cyan]")


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
        _done("Modal credentials are valid")
    else:
        _step_failed("Modal credentials are missing or invalid; run `modal setup`")
        failures += 1

    try:
        secret_names: set[str] = _secret_names()
    except modal.exception.Error as error:
        _step_failed(f"Could not inspect Modal secrets: {escape(str(error))}")
        secret_names = set()
        failures += 1

    pool_files = sorted(pools_dir.glob("*.py"))
    local_pools: list[tuple[Path, Pool]] = []
    for pool_file in pool_files:
        try:
            pool = _pool_from_file(pool_file, scope=scope)
        except (SystemExit, ConfigError) as error:
            _step_failed(escape(str(error)))
            failures += 1
            continue
        local_pools.append((pool_file, pool))
        try:
            failures += _check_local_pool(pool_file, pool, secret_names)
        except ConfigError as error:
            _step_failed(escape(str(error)))
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
            _step_failed(f"Could not read Cursor pool registry: {escape(str(error))}")
            failures += 1
    elif local_pools:
        _warn("CURSOR_API_KEY is not set; skipped Cursor registry and worker checks")

    if registry is not None:
        failures += _check_registry(local_pools, registry, scope)

    if failures:
        raise SystemExit(1)


def run() -> None:
    app()
