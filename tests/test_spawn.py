from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import modal
import pytest
from logfire.testing import CaptureLogfire

from modal_cursor import Pool
from modal_cursor import spawn as spawn_module
from modal_cursor.pools import ConfigError

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
    pool = Pool(name="gpu")
    worker = pool.machine(image="image", gpu="A10G")
    create = Mock(return_value=SimpleNamespace(object_id="sb-1"))
    monkeypatch.setattr(modal.Sandbox, "create", create)
    ready = Mock()
    monkeypatch.setattr(spawn_module, "_wait_for_cursor_registration", ready)
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
        spawn_module._wait_for_cursor_registration(exited, Mock(), "pw-1")

    timed_out = SimpleNamespace(
        object_id="sb-timeout",
        poll=Mock(return_value=None),
        terminate=Mock(),
    )
    monkeypatch.setattr(spawn_module, "worker_connected", lambda client, worker_id: False)
    with pytest.raises(spawn_module.WorkerProvisioningError, match="did not connect"):
        spawn_module._wait_for_cursor_registration(timed_out, Mock(), "pw-1", timeout_s=0)
    timed_out.terminate.assert_called_once_with()


def test_worker_registration_records_semantic_poll_spans(
    monkeypatch: pytest.MonkeyPatch, capfire: CaptureLogfire
) -> None:
    sandbox = SimpleNamespace(object_id="sb-ready", poll=Mock(return_value=None))
    monkeypatch.setattr(spawn_module, "worker_connected", Mock(side_effect=[False, True]))
    monkeypatch.setattr(spawn_module.time, "sleep", Mock())

    spawn_module._wait_for_cursor_registration(sandbox, Mock(), "pw-ready")

    exported = [
        item
        for item in capfire.exporter.exported_spans
        if item.attributes.get("logfire.span_type") == "span"
    ]
    polls = [item for item in exported if item.name == "modal_cursor.worker.registration.poll"]
    assert [item.attributes["modal_cursor.worker.poll.outcome"] for item in polls] == [
        "not_ready",
        "ready",
    ]
    readiness = next(
        item for item in exported if item.name == "modal_cursor.worker.wait_for_cursor_registration"
    )
    assert readiness.attributes["modal_cursor.worker.process_alive"] is True
    assert readiness.attributes["modal_cursor.worker.registration_pending"] is True
    assert readiness.attributes["modal_cursor.worker.registration_outcome"] == "ready"


def test_spawn_worker_rejects_cross_pool_claim() -> None:
    with pytest.raises(ConfigError, match="reached worker provisioner"):
        spawn_module.spawn_worker(
            Pool(name="other"), Pool(name="other").machine(image="image"), "app", CLAIM_PAYLOAD
        )
