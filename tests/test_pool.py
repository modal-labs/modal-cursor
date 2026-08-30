from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from modal_cursor import Pool
from modal_cursor import pool as pool_module


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
    pool = Pool(name="payments", repo_url="https://github.com/acme/payments.git")
    assert pool.repo_url == "https://github.com/acme/payments"
    assert pool.registration.request_body()["workerReadyTimeoutSeconds"] == 0
    with pytest.raises(ValidationError, match="snapshot/restore") as error:
        Pool(name="payments", worker_ready_timeout_s=900)
    assert (
        error.value.errors()[0]["type"] == "pool_worker_ready_timeout_s_requires_snapshot_restore"
    )
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
    ).register()

    body = json.loads(requests[0].content)
    assert body["repoOwner"] == "acme"
    assert body["workerReadyTimeoutSeconds"] == 0


def test_control_plane_image_declares_claim_validation_runtime() -> None:
    assert "pydantic-settings==2.15.0" in pool_module._CONTROLLER_DEPENDENCIES
    assert "logfire[httpx]==4.41.0" in pool_module._CONTROLLER_DEPENDENCIES
