from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from modal_cursor import Pool
from modal_cursor import pool as pool_module
from modal_cursor.pool import SPAWN_BRIDGE_PATH
from modal_cursor.pools import APP_NAME_ENV


@pytest.mark.parametrize("name", ["h100", "gpu-training", "a", "x1-y2", "0-default"])
def test_valid_pool_names(name: str) -> None:
    assert Pool(name=name).name == name


@pytest.mark.parametrize(
    "name",
    ["", "GPU Training", "UPPER", "-leading", "trailing-", "under_score", "a" * 51],
)
def test_invalid_pool_names(name: str) -> None:
    with pytest.raises(ValidationError, match="pool name") as error:
        Pool(name=name)
    assert error.value.errors()[0]["type"] == "pool_name_has_invalid_format"


def test_pool_validates_current_registration_contract() -> None:
    pool = Pool(
        name="payments",
        repo_url="https://github.com/acme/payments.git",
        worker_ready_timeout_s=900,
    )
    assert pool.repo_url == "https://github.com/acme/payments"
    assert pool.registration.request_body()["workerReadyTimeoutSeconds"] == 900
    with pytest.raises(ValidationError, match="HTTPS GitHub") as error:
        Pool(name="bad", repo_url="https://gitlab.com/acme/project")
    assert error.value.errors()[0]["type"] == "pool_repo_url_must_be_github_https"


def test_machine_forwards_modal_options_without_copying_pool_metadata() -> None:
    machine = Pool(name="payments", repo_url="https://github.com/acme/payments").machine(
        image="image", gpu="A10G", region="us-east"
    )
    assert dict(machine.sandbox_options) == {"gpu": "A10G", "region": "us-east"}
    assert not hasattr(machine, "repo_url")
    assert not hasattr(machine, "scope")


def test_register_uses_pool_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"registered": True})

    client = httpx.Client(base_url="https://cursor.test", transport=httpx.MockTransport(handler))
    monkeypatch.setenv("CURSOR_API_KEY", "service-key")
    monkeypatch.setattr(pool_module, "cursor_client", lambda endpoint, token: client)

    Pool(
        name="payments",
        repo_url="https://github.com/acme/payments",
        worker_ready_timeout_s=600,
    ).register()

    body = json.loads(requests[0].content)
    assert body["repoOwner"] == "acme"
    assert body["workerReadyTimeoutSeconds"] == 600


def test_controller_uses_one_real_executable_path(monkeypatch: pytest.MonkeyPatch) -> None:
    process = Mock(pid=123, args=[])
    start = Mock(return_value=process)
    monkeypatch.setattr(pool_module.subprocess, "Popen", start)
    monkeypatch.setenv("CURSOR_API_KEY", "service-key")

    Pool(name="gpu").start_controller()

    argv = start.call_args.args[0]
    spawn_index = argv.index("--spawn") + 1
    assert argv[0] == "/root/.local/bin/agent"
    assert argv[spawn_index] == SPAWN_BRIDGE_PATH
    assert " " not in argv[spawn_index]
    assert start.call_args.kwargs["env"][APP_NAME_ENV] == "modal-cursor-gpu"
    assert start.call_args.kwargs["env"]["traceparent"].startswith("00-")


def test_spawn_bridge_source_is_an_executable_python_script() -> None:
    bridge = Path(pool_module.__file__).with_name("_spawn_bridge.py")
    assert bridge.read_text().startswith("#!/usr/bin/env python3\n")


def test_controller_image_declares_claim_validation_runtime() -> None:
    assert "pydantic-settings==2.15.0" in pool_module._CONTROLLER_DEPENDENCIES
    assert "logfire[httpx]==4.41.0" in pool_module._CONTROLLER_DEPENDENCIES
