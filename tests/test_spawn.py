from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import modal
import pytest

from modal_cursor import Pool
from modal_cursor import spawn as spawn_module
from modal_cursor.pools import APP_NAME_ENV, ConfigError

CLAIM_ENV = {
    "CURSOR_AGENT_WORKER_ID": "pw-42",
    "CURSOR_POOL": "gpu",
    "CURSOR_REQUEST_ID": "bc-42",
    "CURSOR_API_KEY": "long-lived-service-key",
}
CLAIM_PAYLOAD = {
    "agent_worker_id": "pw-42",
    "pool": "gpu",
    "request_id": "bc-42",
    "worker_name": None,
    "repo_url": None,
    "repo_urls": [],
}


def test_spawn_worker_provisions_sandbox_from_one_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = Pool("gpu")
    worker = pool.machine(image="image", gpu="A10G")
    create = Mock(return_value=SimpleNamespace(object_id="sb-1"))
    monkeypatch.setattr(modal.Sandbox, "create", create)
    ready = Mock()
    monkeypatch.setattr(spawn_module, "_wait_for_worker_ready", ready)
    monkeypatch.setenv("CURSOR_API_KEY", "long-lived-service-key")

    sandbox_id = spawn_module.spawn_worker(pool, worker, "app", CLAIM_PAYLOAD)

    assert sandbox_id == "sb-1"
    args, kwargs = create.call_args
    assert args[:2] == ("bash", "-lc")
    assert kwargs["gpu"] == "A10G"
    assert kwargs["env"]["CURSOR_API_KEY"] == "long-lived-service-key"
    ready.assert_called_once()


def test_worker_readiness_detects_early_exit_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exited = SimpleNamespace(object_id="sb-exited", poll=Mock(return_value=127))
    with pytest.raises(spawn_module.WorkerProvisioningError, match="status 127"):
        spawn_module._wait_for_worker_ready(exited, Mock(), "pw-1")

    timed_out = SimpleNamespace(
        object_id="sb-timeout",
        poll=Mock(return_value=None),
        terminate=Mock(),
    )
    monkeypatch.setattr(spawn_module, "worker_connected", lambda client, worker_id: False)
    with pytest.raises(spawn_module.WorkerProvisioningError, match="did not connect"):
        spawn_module._wait_for_worker_ready(timed_out, Mock(), "pw-1", timeout_s=0)
    timed_out.terminate.assert_called_once_with()


def test_spawn_worker_rejects_cross_pool_claim() -> None:
    with pytest.raises(ConfigError, match="reached spawner"):
        spawn_module.spawn_worker(
            Pool("other"), Pool("other").machine(image="image"), "app", CLAIM_PAYLOAD
        )


def test_bridge_waits_for_remote_provisioning_without_forwarding_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in CLAIM_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(APP_NAME_ENV, "modal-cursor-gpu")
    spawner = Mock()
    spawner.remote.return_value = "sb-1"
    monkeypatch.setattr(modal.Function, "from_name", Mock(return_value=spawner))

    assert spawn_module.main() == 0

    payload = spawner.remote.call_args.args[0]
    assert payload["pool"] == "gpu"
    assert "api_key" not in payload
    assert "long-lived-service-key" not in repr(payload)
    assert not spawner.spawn.called


def test_bridge_releases_claim_when_modal_provisioning_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in CLAIM_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(APP_NAME_ENV, "modal-cursor-gpu")
    monkeypatch.setattr(
        modal.Function,
        "from_name",
        Mock(return_value=Mock(remote=Mock(side_effect=modal.exception.RemoteError("boom")))),
    )
    release = Mock(return_value=None)
    monkeypatch.setattr(spawn_module, "_release_failed_claim", release)

    assert spawn_module.main() == 1
    release.assert_called_once()
    assert release.call_args.args[0].request_id == "bc-42"


def test_bridge_requires_complete_claim_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in CLAIM_ENV:
        monkeypatch.delenv(key, raising=False)
    assert spawn_module.main() == 2
