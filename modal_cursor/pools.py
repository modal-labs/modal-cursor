"""Validated Cursor claim settings and deterministic worker construction."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, LiteralString, Self, TypeVar, cast
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    import modal

    Image = modal.Image
    Secret = modal.Secret
else:
    Image = object
    Secret = object

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


class RuntimeSettings(BaseSettings):
    """Operator-tunable worker lifecycle defaults."""

    model_config = SettingsConfigDict(env_prefix="MODAL_CURSOR_", extra="ignore")

    sandbox_timeout_s: Annotated[
        int, Field(gt=0, description="Maximum lifetime of each Modal worker sandbox in seconds.")
    ] = 6 * 3600
    idle_release_timeout_s: Annotated[
        int, Field(ge=0, description="Seconds of worker idleness before the sandbox is released.")
    ] = 600
    spawner_ready_timeout_s: Annotated[
        float, Field(ge=0, description="Seconds to wait for Cursor to observe a new worker.")
    ] = 120
    worker_poll_interval_s: Annotated[
        float, Field(gt=0, description="Seconds between Cursor worker readiness checks.")
    ] = 1.0
    controller_timeout_s: Annotated[
        int, Field(gt=0, description="Maximum runtime of a controller invocation in seconds.")
    ] = 24 * 3600
    controller_max_retries: Annotated[
        int, Field(ge=0, description="Maximum number of retries for a controller invocation.")
    ] = 10


def _reject_reserved(
    values: Mapping[str, object], reserved: set[str], code: LiteralString, message: str
) -> None:
    names = sorted(reserved & values.keys())
    if names:
        raise PydanticCustomError(
            code,
            "{message}: {names}",
            {"message": message, "names": ", ".join(names)},
        )


_MappingValue = TypeVar("_MappingValue")


def _freeze_mapping(value: Mapping[str, _MappingValue]) -> Mapping[str, _MappingValue]:
    return cast(Mapping[str, _MappingValue], MappingProxyType(dict(value)))


FrozenEnvironment = Annotated[Mapping[str, str], AfterValidator(_freeze_mapping)]
FrozenSandboxOptions = Annotated[Mapping[str, object], AfterValidator(_freeze_mapping)]


class Machine(BaseModel):
    """Modal sandbox configuration that is independent of pool registration."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True, validate_default=True)

    image: Image
    secrets: tuple[Secret, ...] = ()
    env: FrozenEnvironment = Field(default_factory=dict[str, str])
    timeout_s: int = Field(default_factory=lambda: RuntimeSettings().sandbox_timeout_s)
    idle_release_s: int = Field(default_factory=lambda: RuntimeSettings().idle_release_timeout_s)
    sandbox_options: FrozenSandboxOptions = Field(default_factory=dict[str, object])

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        if self.timeout_s <= 0:
            raise PydanticCustomError(
                "machine_timeout_s_must_be_positive", "timeout_s must be greater than zero"
            )
        if self.idle_release_s < 0:
            raise PydanticCustomError(
                "machine_idle_release_s_must_be_non_negative",
                "idle_release_s must be zero or greater",
            )
        _reject_reserved(
            self.env,
            _RESERVED_WORKER_ENV,
            "machine_env_contains_reserved_variable",
            "env cannot override Cursor-managed variables",
        )
        _reject_reserved(
            self.sandbox_options,
            _RESERVED_SANDBOX_OPTIONS,
            "machine_sandbox_options_contains_reserved_option",
            "sandbox options are managed by modal-cursor",
        )
        return self


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
    urls = (
        claim.repo_urls
        or ((claim.repo_url,) if claim.repo_url else ())
        or ((default_repo_url,) if default_repo_url else ())
    )
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
    values = {
        **machine.env,
        "CURSOR_AGENT_WORKER_ID": claim.agent_worker_id,
        "CURSOR_API_KEY": api_key,
        "CURSOR_POOL": claim.pool,
        "CURSOR_REQUEST_ID": claim.request_id,
        "CURSOR_WORKER_IDLE_RELEASE_TIMEOUT": str(machine.idle_release_s),
        **({"CURSOR_WORKER_NAME": claim.worker_name} if claim.worker_name else {}),
        **({"CURSOR_REPO_URL": claim.repo_url} if claim.repo_url else {}),
        **({"CURSOR_REPO_URLS": json.dumps(claim.repo_urls)} if claim.repo_urls else {}),
    }
    return values
