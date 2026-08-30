"""The public Pool API and its Modal image/runtime integration."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

import modal
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic_core import PydanticCustomError

from modal_cursor.pools import (
    APP_NAME_ENV,
    CURSOR_AGENT_PATH,
    Machine,
    RuntimeSettings,
)
from modal_cursor.registry import (
    DEFAULT_API_ENDPOINT,
    PoolRegistration,
    Repository,
    cursor_client,
    register_pool,
)
from modal_cursor.telemetry import current_span, inject_trace_context, instrument, set_attribute

MAX_NAME_LENGTH = 50  # "modal-cursor-" + 50 characters fits Modal's 64-char cap.
SPAWN_BRIDGE_PATH = "/usr/local/bin/modal-cursor-spawn"

_CURSOR_CLI_RELEASE = "2026.08.28-8fddf07"
_CURSOR_CLI_URL = (
    f"https://downloads.cursor.com/lab/{_CURSOR_CLI_RELEASE}/linux/x64/agent-cli-package.tar.gz"
)
_CURSOR_CLI_SHA256 = "76c213c284647a1cf5fb47897e1bb1953e89b345c048ca869135d4b556e0b859"
_CONTROLLER_DEPENDENCIES = (
    "httpx==0.28.1",
    "logfire[httpx]==4.41.0",
    "modal==1.5.4",
    "pydantic-settings==2.15.0",
)
_POOL_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
_BRIDGE_SOURCE = Path(__file__).with_name("_spawn_bridge.py")


def _cursor_cli_image(*extra_apt: str) -> modal.Image:
    """Build a reproducible Cursor CLI base image from a versioned release URL."""
    install = (
        "set -eux; "
        "mkdir -p /opt/cursor-agent /root/.local/bin; "
        f"curl -fsSL {_CURSOR_CLI_URL} -o /tmp/cursor-agent.tar.gz; "
        f"echo '{_CURSOR_CLI_SHA256}  /tmp/cursor-agent.tar.gz' | sha256sum -c -; "
        "tar -xzf /tmp/cursor-agent.tar.gz --strip-components=1 -C /opt/cursor-agent; "
        "rm /tmp/cursor-agent.tar.gz; "
        "ln -s /opt/cursor-agent/cursor-agent /root/.local/bin/agent"
    )
    return (
        modal.Image.debian_slim()
        .apt_install("curl", "ca-certificates", *extra_apt)
        .run_commands(install)
        .env({"PATH": "/root/.local/bin:$PATH"})
    )


class Pool(BaseModel):
    """One Cursor routing pool and its canonical Modal application identity."""

    model_config = ConfigDict(frozen=True, strict=True)

    name: str
    repo_url: str | None = None
    scope: Literal["team", "user"] = "team"
    worker_ready_timeout_s: int = 0
    api_endpoint: str = DEFAULT_API_ENDPOINT

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not (0 < len(value) <= MAX_NAME_LENGTH) or not _POOL_NAME_RE.fullmatch(value):
            raise PydanticCustomError(
                "pool_name_has_invalid_format",
                "pool name {name!r} must be 1-{max_length} chars of lowercase "
                "letters, digits, and dashes, starting and ending with a letter or digit",
                {"name": value, "max_length": MAX_NAME_LENGTH},
            )
        return value

    @field_validator("repo_url")
    @classmethod
    def _normalize_repo_url(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                return Repository.from_url(value).url
            except ValueError as error:
                raise PydanticCustomError(
                    "pool_repo_url_must_be_github_https",
                    "invalid repository URL: {reason}",
                    {"reason": str(error)},
                ) from error
        return None

    @model_validator(mode="after")
    def _valid_values(self) -> Pool:
        if self.worker_ready_timeout_s < 0:
            raise PydanticCustomError(
                "pool_worker_ready_timeout_s_must_be_non_negative",
                "worker_ready_timeout_s must be zero or greater",
            )
        return self

    @property
    def app_name(self) -> str:
        return f"modal-cursor-{self.name}"

    @property
    def registration(self) -> PoolRegistration:
        repository = Repository.from_url(self.repo_url) if self.repo_url else None
        return PoolRegistration(
            name=self.name,
            scope=self.scope,
            worker_ready_timeout_s=self.worker_ready_timeout_s,
            repository=repository,
        )

    def controller_image(self) -> modal.Image:
        """Cursor CLI plus the exact dependencies and executable spawn bridge."""
        return (
            _cursor_cli_image()
            .uv_pip_install(*_CONTROLLER_DEPENDENCIES)
            .add_local_python_source("modal_cursor", copy=True)
            .add_local_file(_BRIDGE_SOURCE, SPAWN_BRIDGE_PATH, copy=True)
            .run_commands(f"chmod 755 {SPAWN_BRIDGE_PATH}")
        )

    def worker_image(self) -> modal.Image:
        """Cursor CLI and git; callers may append application-specific layers."""
        return _cursor_cli_image("git")

    def machine(
        self,
        *,
        image: modal.Image,
        secrets: Sequence[modal.Secret] = (),
        env: Mapping[str, str] | None = None,
        timeout_s: int | None = None,
        idle_release_s: int | None = None,
        **sandbox_options: object,
    ) -> Machine:
        """Bind worker lifecycle policy while forwarding Modal sandbox options."""
        settings = RuntimeSettings()
        return Machine(
            image=image,
            secrets=tuple(secrets),
            env={} if env is None else env,
            timeout_s=settings.sandbox_timeout_s if timeout_s is None else timeout_s,
            idle_release_s=(
                settings.idle_release_timeout_s if idle_release_s is None else idle_release_s
            ),
            sandbox_options=sandbox_options,
        )

    @instrument("modal_cursor.pool.register")
    def register(self) -> None:
        """Register this pool from its own metadata before the controller starts."""
        current = current_span()
        set_attribute(current, "modal_cursor.pool.name", self.name)
        set_attribute(current, "modal_cursor.pool.scope", self.scope)
        set_attribute(current, "modal_cursor.pool.repository_scoped", self.repo_url is not None)
        endpoint = os.environ.get("CURSOR_API_ENDPOINT", self.api_endpoint)
        with cursor_client(endpoint, os.environ["CURSOR_API_KEY"]) as client:
            register_pool(client, self.registration)

    @instrument("modal_cursor.controller.run")
    def start_controller(self) -> subprocess.Popen[bytes]:
        """Start Cursor's controller and return before its long-lived process ends."""
        current = current_span()
        set_attribute(current, "modal_cursor.pool.name", self.name)
        set_attribute(current, "modal_cursor.app.name", self.app_name)
        env = {**os.environ, APP_NAME_ENV: self.app_name}
        inject_trace_context(env)
        process = subprocess.Popen(
            [
                CURSOR_AGENT_PATH,
                "worker",
                "controller",
                "--spawn",
                SPAWN_BRIDGE_PATH,
                "--pool",
                self.name,
            ],
            env=env,
        )
        set_attribute(current, "process.pid", process.pid)
        return process

    def run_controller(self) -> None:
        """Run Cursor's controller, waiting after its startup span is exported."""
        process = self.start_controller()
        returncode = process.wait()
        if returncode:
            raise subprocess.CalledProcessError(returncode, process.args)
