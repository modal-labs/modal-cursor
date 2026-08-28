from __future__ import annotations

import subprocess
import sys

import pytest
from pydantic import ValidationError

from modal_cursor.pools import Claim, ConfigError, Machine, build_entrypoint, build_worker_env


def _claim(**overrides: object) -> Claim:
    values: dict[str, object] = {
        "agent_worker_id": "pw-42",
        "pool": "payments",
        "request_id": "bc-42",
    }
    values.update(overrides)
    return Claim.model_validate(values)


def test_claim_reads_required_cursor_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_AGENT_WORKER_ID", "pw-1")
    monkeypatch.setenv("CURSOR_POOL", "gpu")
    monkeypatch.setenv("CURSOR_REQUEST_ID", "bc-1")
    monkeypatch.setenv(
        "CURSOR_REPO_URLS",
        '["https://github.com/acme/app", "https://github.com/acme/infra"]',
    )
    monkeypatch.setenv("UNRELATED_SECRET", "ignored")
    claim = Claim()  # type: ignore[call-arg]
    assert claim.pool == "gpu"
    assert claim.repo_urls == (
        "https://github.com/acme/app",
        "https://github.com/acme/infra",
    )


@pytest.mark.parametrize("missing", ["agent_worker_id", "pool", "request_id"])
def test_claim_requires_complete_claim_identity(missing: str) -> None:
    values = {"agent_worker_id": "pw", "pool": "gpu", "request_id": "bc"}
    del values[missing]
    with pytest.raises(ValidationError):
        Claim.model_validate(values)


def test_claim_payload_never_contains_service_account_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "long-lived-secret")
    payload = _claim().payload()
    assert "api_key" not in payload
    assert "CURSOR_API_KEY" not in payload
    assert "long-lived-secret" not in repr(payload)


def test_machine_is_immutable_and_rejects_reserved_overrides() -> None:
    source = {"APP_ENV": "production"}
    machine = Machine(image="image", env=source, sandbox_options={"gpu": "A10G"})
    source["APP_ENV"] = "changed"
    assert machine.env["APP_ENV"] == "production"
    with pytest.raises(TypeError):
        machine.env["APP_ENV"] = "changed"  # type: ignore[index]
    with pytest.raises(ConfigError, match="CURSOR_API_KEY"):
        Machine(image="image", env={"CURSOR_API_KEY": "wrong"})
    with pytest.raises(ConfigError, match="managed"):
        Machine(image="image", sandbox_options={"timeout": 30})


def test_worker_environment_preserves_claim_identity() -> None:
    machine = Machine(image="image", env={"APP_ENV": "production"}, idle_release_s=60)
    env = build_worker_env(machine, _claim(worker_name="worker-1"), "service-key")
    assert env == {
        "APP_ENV": "production",
        "CURSOR_AGENT_WORKER_ID": "pw-42",
        "CURSOR_API_KEY": "service-key",
        "CURSOR_POOL": "payments",
        "CURSOR_REQUEST_ID": "bc-42",
        "CURSOR_WORKER_IDLE_RELEASE_TIMEOUT": "60",
        "CURSOR_WORKER_NAME": "worker-1",
    }


def test_repo_entrypoint_clones_then_starts_worker() -> None:
    command = build_entrypoint(
        "payments",
        _claim(repo_url="https://github.com/acme/payments"),
        default_repo_url=None,
    )
    assert command[:2] == ("bash", "-lc")
    assert "git clone --depth 50" in command[2]
    assert "GITHUB_TOKEN" in command[2]
    assert "exec /root/.local/bin/agent worker" in command[2]
    assert "--worker-dir /workspace/payments" in command[2]


def test_repo_entrypoint_uses_pool_repo_as_fallback() -> None:
    script = build_entrypoint("payments", _claim(), "https://github.com/acme/payments")[2]
    assert "https://github.com/acme/payments" in script


def test_repo_limits_fail_instead_of_silently_dropping_work() -> None:
    urls = tuple(f"https://github.com/acme/repo-{index}" for index in range(21))
    with pytest.raises(ValidationError, match="at most 20"):
        _claim(repo_urls=urls)


def test_repo_destination_collisions_fail_before_provisioning() -> None:
    claim = _claim(
        repo_urls=(
            "https://github.com/acme/app",
            "https://github.com/another/app",
        )
    )
    with pytest.raises(ConfigError, match="collide"):
        build_entrypoint("payments", claim, None)


def test_core_module_does_not_import_modal() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import modal_cursor.pools; print('modal' in sys.modules)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "False"
