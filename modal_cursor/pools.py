"""Validated Cursor claim settings and deterministic worker construction."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    import modal

DEFAULT_TIMEOUT_S = 6 * 3600
DEFAULT_IDLE_RELEASE_S = 600
MAX_REPOSITORIES = 20
WORKSPACE = "/workspace"
CURSOR_AGENT_PATH = "/root/.local/bin/agent"

APP_NAME_ENV = "MODAL_CURSOR_APP_NAME"

_RESERVED_WORKER_ENV = {
    "CURSOR_AGENT_WORKER_ID",
    "CURSOR_API_KEY",
    "CURSOR_POOL",
    "CURSOR_REPO_URL",
    "CURSOR_REPO_URLS",
    "CURSOR_REQUEST_ID",
    "CURSOR_WORKER_IDLE_RELEASE_TIMEOUT",
    "CURSOR_WORKER_NAME",
}
_RESERVED_SANDBOX_OPTIONS = {"app", "env", "image", "secrets", "timeout"}


class ConfigError(ValueError):
    """Local pool or claim configuration violates a required contract."""


@dataclass(frozen=True, slots=True)
class Machine:
    """Modal sandbox configuration that is independent of pool registration."""

    image: modal.Image
    secrets: tuple[modal.Secret, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_s: int = DEFAULT_TIMEOUT_S
    idle_release_s: int = DEFAULT_IDLE_RELEASE_S
    sandbox_options: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timeout_s <= 0:
            raise ConfigError("timeout_s must be greater than zero")
        if self.idle_release_s < 0:
            raise ConfigError("idle_release_s must be zero or greater")
        env = dict(self.env)
        reserved_env = sorted(_RESERVED_WORKER_ENV & env.keys())
        if reserved_env:
            raise ConfigError(
                "env cannot override Cursor-managed variables: " + ", ".join(reserved_env)
            )
        options = dict(self.sandbox_options)
        reserved_options = sorted(_RESERVED_SANDBOX_OPTIONS & options.keys())
        if reserved_options:
            raise ConfigError(
                "sandbox options are managed by modal-cursor: " + ", ".join(reserved_options)
            )
        object.__setattr__(self, "env", MappingProxyType(env))
        object.__setattr__(self, "sandbox_options", MappingProxyType(options))


class Claim(BaseSettings):
    """Required non-secret values Cursor exports after a successful claim."""

    model_config = SettingsConfigDict(env_prefix="CURSOR_", extra="forbid")

    agent_worker_id: str
    pool: str
    request_id: str
    worker_name: str | None = None
    repo_url: str | None = None
    repo_urls: tuple[str, ...] = ()

    @field_validator("agent_worker_id", "pool", "request_id")
    @classmethod
    def _required_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("repo_urls")
    @classmethod
    def _valid_repositories(cls, urls: tuple[str, ...]) -> tuple[str, ...]:
        if len(urls) > MAX_REPOSITORIES:
            raise ValueError(f"a claim may contain at most {MAX_REPOSITORIES} repositories")
        if any(not url.strip() for url in urls):
            raise ValueError("repository URLs must be non-empty strings")
        return urls

    def payload(self) -> dict[str, object]:
        """Serialize only non-secret fields for the deployed Modal spawner."""
        return self.model_dump()


def _checkout_name(url: str) -> str:
    path = urlsplit(url).path.rstrip("/").removesuffix(".git")
    name = path.rsplit("/", 1)[-1]
    if not name or name in {".", ".."}:
        raise ConfigError(f"cannot derive a checkout directory from repository URL {url!r}")
    return name


def build_entrypoint(pool_name: str, claim: Claim, default_repo_url: str | None) -> tuple[str, ...]:
    """Build the worker command, rejecting ambiguous repository destinations."""
    urls = claim.repo_urls or ((claim.repo_url,) if claim.repo_url else ())
    if not urls and default_repo_url:
        urls = (default_repo_url,)
    destinations = [_checkout_name(url) for url in urls]
    duplicates = sorted({name for name in destinations if destinations.count(name) > 1})
    if duplicates:
        raise ConfigError("repository checkout names collide: " + ", ".join(duplicates))

    lines = ["set -euo pipefail"]
    args = [CURSOR_AGENT_PATH, "worker", "--pool", shlex.quote(pool_name)]
    if claim.worker_name:
        args += ["--name", shlex.quote(claim.worker_name)]
    if urls:
        lines.append(f"mkdir -p {WORKSPACE}")
        for url, destination in zip(urls, destinations, strict=True):
            checkout = f"{WORKSPACE}/{destination}"
            tokenized_url = 'URL="https://x-access-token:${GITHUB_TOKEN}@${URL#https://}"'
            lines.append(
                f"URL={shlex.quote(url)}; "
                f'[ -n "${{GITHUB_TOKEN:-}}" ] && {tokenized_url}; '
                f'git clone --depth 50 "$URL" {shlex.quote(checkout)}'
            )
            args += ["--worker-dir", shlex.quote(checkout)]
    lines.append(f"exec {' '.join(args)} start")
    return "bash", "-lc", "\n".join(lines)


def build_worker_env(machine: Machine, claim: Claim, api_key: str) -> dict[str, str]:
    """Build the exact environment exposed to the Cursor worker process."""
    if not api_key:
        raise ConfigError("CURSOR_API_KEY is required in the spawner secret")
    values = dict(machine.env)
    values.update(
        CURSOR_AGENT_WORKER_ID=claim.agent_worker_id,
        CURSOR_API_KEY=api_key,
        CURSOR_POOL=claim.pool,
        CURSOR_REQUEST_ID=claim.request_id,
        CURSOR_WORKER_IDLE_RELEASE_TIMEOUT=str(machine.idle_release_s),
    )
    if claim.worker_name:
        values["CURSOR_WORKER_NAME"] = claim.worker_name
    if claim.repo_url:
        values["CURSOR_REPO_URL"] = claim.repo_url
    if claim.repo_urls:
        values["CURSOR_REPO_URLS"] = json.dumps(claim.repo_urls)
    return values
