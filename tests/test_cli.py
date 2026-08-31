from __future__ import annotations

import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import modal
import pytest

from modal_cursor import cli
from modal_cursor.pool import Pool
from modal_cursor.pools import ConfigError
from modal_cursor.registry import RegisteredPool


def _init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **kwargs: object,
) -> Path:
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(
        cli, "_secret_names", lambda: {"cursor-service-account", "github-token", "logfire-token"}
    )
    cli.init_pool(name="demo-pool", pools_dir=tmp_path, **kwargs)
    return tmp_path / "demo-pool.py"


def _registry_payload(
    name: str = "demo-pool", *, connected: int = 0, repo_url: str | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "scope": "team",
        "poolName": name,
        "connectedWorkerCount": connected,
        "inUseWorkerCount": 0,
        "workerReadyTimeoutSeconds": 0,
    }
    if repo_url is not None:
        payload.update(
            repoOwner="acme",
            repoName="app",
            repoUrl=repo_url,
        )
    return payload


def test_init_generates_pool_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generated = _init(
        tmp_path,
        monkeypatch,
        repo_url="https://github.com/acme/app",
    )
    namespace = runpy.run_path(str(generated))
    source = generated.read_text()

    assert "pool.register()" not in source
    assert "SpawnServer(" not in source
    assert "spawn_worker(" not in source
    assert "app =" not in source
    assert namespace["pool"].worker_ready_timeout_s == 0
    assert namespace["CURSOR_SECRET_NAME"] == "cursor-service-account"
    assert namespace["WORKER_SECRET_NAMES"] == ()
    assert namespace["worker"].image is not None


def test_private_repo_configures_github_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated = _init(
        tmp_path,
        monkeypatch,
        repo_url="https://github.com/acme/private",
        private_repo=True,
    )
    namespace = runpy.run_path(str(generated))
    assert namespace["WORKER_SECRET_NAMES"] == ("github-token",)
    assert len(namespace["worker"].secrets) == 1
    assert cli._required_secrets(generated) == {
        "cursor-service-account",
        "github-token",
    }


def test_init_rejects_invalid_repo_and_private_repo_without_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(SystemExit, match="HTTPS GitHub"):
        _init(tmp_path, monkeypatch, repo_url="https://gitlab.com/acme/app")
    with pytest.raises(SystemExit, match="requires --repo-url"):
        _init(tmp_path, monkeypatch, private_repo=True)


def test_init_refuses_to_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="already exists"):
        _init(tmp_path, monkeypatch)


def test_deploy_starts_single_control_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(tmp_path, monkeypatch)
    deployed: list[list[Path]] = []
    monkeypatch.setattr(cli, "_modal_deploy", lambda files: deployed.append(list(files)) or 0)
    controller = Mock()
    from_name = Mock(return_value=controller)
    monkeypatch.setattr(cli.modal.Function, "from_name", from_name)

    cli.deploy(pools_dir=tmp_path)

    from_name.assert_called_once_with(cli.CONTROL_PLANE_APP_NAME, "controller")
    controller.spawn.assert_called_once_with()
    assert deployed == [[tmp_path / "demo-pool.py"]]


def test_subprocess_boundaries_and_configuration_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
    monkeypatch.setattr(cli.subprocess, "run", run)

    assert cli._modal("token", "info").returncode == 0
    assert cli._modal_deploy(tmp_path / "gpu.py") == 0
    assert cli._modal_is_configured()
    assert run.call_args_list[0].kwargs == {
        "capture_output": True,
        "text": True,
        "timeout": 30,
        "check": False,
    }
    assert cli.POOL_FILES_ENV in run.call_args_list[1].kwargs["env"]

    monkeypatch.setattr(cli, "_modal", Mock(side_effect=subprocess.TimeoutExpired("modal", 30)))
    assert not cli._modal_is_configured()


def test_pool_file_and_secret_validation(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="No pool files"):
        cli._pool_files(None, tmp_path)
    with pytest.raises(SystemExit, match="not a valid pool file name"):
        cli._pool_from_file(tmp_path / "Bad_Name.py")

    invalid = tmp_path / "invalid.py"
    invalid.write_text("not python (")
    with pytest.raises(ConfigError, match="cannot inspect"):
        cli._required_secrets(invalid)

    missing = tmp_path / "missing.py"
    missing.write_text("WORKER_SECRET_NAMES = ()")
    with pytest.raises(ConfigError, match="CURSOR_SECRET_NAME is missing"):
        cli._required_secrets(missing)

    dynamic = tmp_path / "dynamic.py"
    dynamic.write_text("CURSOR_SECRET_NAME = make_secret()")
    with pytest.raises(ConfigError, match="must be a literal"):
        cli._required_secrets(dynamic)

    malformed = tmp_path / "malformed.py"
    malformed.write_text('CURSOR_SECRET_NAME = "cursor"\nWORKER_SECRET_NAMES = "github"')
    with pytest.raises(ConfigError, match="must be a sequence"):
        cli._required_secrets(malformed)


def test_deploy_rejects_invalid_pool_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = tmp_path / "good.py"
    bad = tmp_path / "Bad_Name.py"
    good.touch()
    bad.touch()
    with pytest.raises(SystemExit, match="not a valid pool file name"):
        cli.deploy(pools_dir=tmp_path)


def test_stop_control_plane_handles_absence_sdk_and_cli_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = Mock(side_effect=modal.exception.NotFoundError("missing"))
    monkeypatch.setattr(cli.modal.App, "lookup", lookup)
    assert cli._stop_control_plane_app()

    lookup.side_effect = modal.exception.RemoteError("unavailable")
    assert not cli._stop_control_plane_app()

    lookup.side_effect = None
    lookup.return_value = object()
    monkeypatch.setattr(
        cli,
        "_modal",
        lambda *args: subprocess.CompletedProcess(args, 1, "", "stop failed"),
    )
    assert not cli._stop_control_plane_app()

    monkeypatch.setattr(
        cli,
        "_modal",
        lambda *args: subprocess.CompletedProcess(args, 0, "", ""),
    )
    assert cli._stop_control_plane_app()


def test_deregister_matches_handles_absent_and_failed_records() -> None:
    pool = Pool(name="gpu")
    client = httpx.Client(
        base_url="https://cursor.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    assert cli._deregister_matches(client, pool, "team", [])
    registered = RegisteredPool.from_payload(
        {
            "scope": "team",
            "poolName": "gpu",
            "connectedWorkerCount": 0,
            "inUseWorkerCount": 0,
            "workerReadyTimeoutSeconds": 0,
        }
    )
    assert not cli._deregister_matches(client, pool, "team", [registered])
    client.close()


def test_destroy_uses_registry_repo_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(tmp_path, monkeypatch, repo_url="https://github.com/acme/app")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200, json={"pools": [_registry_payload(repo_url="https://github.com/acme/app")]}
            )
        return httpx.Response(200, json={"deregistered": True})

    client = httpx.Client(base_url="https://cursor.test", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(cli, "cursor_client", lambda endpoint, token: client)
    monkeypatch.setattr(
        cli.modal.App,
        "lookup",
        Mock(side_effect=modal.exception.NotFoundError("not found")),
    )
    monkeypatch.setenv("CURSOR_API_KEY", "service-key")

    cli.destroy(pools_dir=tmp_path, yes=True)

    delete = next(request for request in requests if request.method == "DELETE")
    assert delete.url.params["repo_owner"] == "acme"
    assert delete.url.params["repo_name"] == "app"


def test_destroy_reads_registry_before_stopping_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(tmp_path, monkeypatch)
    client = httpx.Client(
        base_url="https://cursor.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    monkeypatch.setattr(cli, "cursor_client", lambda endpoint, token: client)
    lookup = Mock()
    monkeypatch.setattr(cli.modal.App, "lookup", lookup)
    monkeypatch.setenv("CURSOR_API_KEY", "service-key")

    with pytest.raises(SystemExit, match="nothing was changed"):
        cli.destroy(pools_dir=tmp_path, yes=True)
    lookup.assert_not_called()


def test_destroy_requires_confirmation_and_service_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="requires --yes"):
        cli.destroy(pools_dir=tmp_path)
    with pytest.raises(SystemExit, match="CURSOR_API_KEY is required"):
        cli.destroy(pools_dir=tmp_path, yes=True)


def test_registry_drift_counts_both_directions(tmp_path: Path) -> None:
    registered = RegisteredPool.from_payload(_registry_payload(name="orphan"))
    failures = cli._check_registry(
        [(tmp_path / "missing.py", Pool(name="missing"))], [registered], "team"
    )
    assert failures == 2


def test_doctor_checks_controller_runner_and_worker_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_modal_is_configured", lambda: True)
    monkeypatch.setattr(cli.modal.App, "lookup", Mock(return_value=object()))
    function = Mock()
    function.get_current_stats.return_value = SimpleNamespace(num_total_runners=1)
    monkeypatch.setattr(cli.modal.Function, "from_name", Mock(return_value=function))
    client = httpx.Client(
        base_url="https://cursor.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"pools": [_registry_payload(connected=2)]})
        ),
    )
    monkeypatch.setattr(cli, "cursor_client", lambda endpoint, token: client)
    monkeypatch.setenv("CURSOR_API_KEY", "service-key")

    cli.doctor(pools_dir=tmp_path)


def test_doctor_fails_for_deployed_app_without_controller_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_modal_is_configured", lambda: True)
    monkeypatch.setattr(cli.modal.App, "lookup", Mock(return_value=object()))
    function = Mock()
    function.get_current_stats.return_value = SimpleNamespace(num_total_runners=0)
    monkeypatch.setattr(cli.modal.Function, "from_name", Mock(return_value=function))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)

    with pytest.raises(SystemExit) as error:
        cli.doctor(pools_dir=tmp_path)
    assert error.value.code == 1


def test_doctor_detects_registry_configuration_drift(tmp_path: Path, monkeypatch) -> None:
    _init(tmp_path, monkeypatch, repo_url="https://github.com/acme/app")
    registered = RegisteredPool.from_payload(
        _registry_payload(repo_url="https://github.com/acme/app")
        | {"workerReadyTimeoutSeconds": 900}
    )
    failures = cli._check_registry(
        [
            (
                tmp_path / "demo-pool.py",
                Pool(name="demo-pool", repo_url="https://github.com/acme/app"),
            )
        ],
        [registered],
        "team",
    )
    assert failures == 1
