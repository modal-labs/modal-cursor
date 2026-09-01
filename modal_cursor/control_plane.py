"""The single Modal control plane for every configured Cursor worker pool."""

from __future__ import annotations

import os
import runpy
from dataclasses import dataclass
from pathlib import Path

import modal

from modal_cursor.controller import PoolSpec, run_control_plane
from modal_cursor.pool import Pool
from modal_cursor.pools import Machine, RuntimeSettings

CONTROL_PLANE_APP_NAME = "modal-cursor-control-plane"
POOL_FILES_ENV = "MODAL_CURSOR_POOL_FILES"
_BUNDLED_POOL_DIR = Path("/root/modal-cursor-pools")


@dataclass(frozen=True)
class _PoolConfig:
    path: Path
    spec: PoolSpec
    cursor_secret_name: str


def _configured_paths() -> tuple[Path, ...]:
    configured = os.environ.get(POOL_FILES_ENV)
    if configured:
        return tuple(Path(item).resolve() for item in configured.split(os.pathsep) if item)
    # The bundled directory exists only in the deployed function image. Do not
    # inspect it while the app module is being imported: on some hosts, merely
    # stat-ing /root is not permitted. Local execution has its own explicit
    # fallback, and the deployed function discovers bundled files at invoke time.
    return tuple(sorted(Path("pools").glob("*.py")))


def _bundled_paths() -> tuple[Path, ...]:
    """Find pool files after Modal has mounted the function image."""
    return tuple(sorted(_BUNDLED_POOL_DIR.glob("*.py")))


def _load_config(path: Path) -> _PoolConfig:
    namespace = runpy.run_path(str(path))
    pool = namespace.get("pool")
    worker = namespace.get("worker")
    cursor_secret_name = namespace.get("CURSOR_SECRET_NAME")
    if not isinstance(pool, Pool):
        raise ValueError(f"{path} must define a modal_cursor.pool.Pool named 'pool'")
    if not isinstance(worker, Machine):
        raise ValueError(f"{path} must define a modal_cursor.pools.Machine named 'worker'")
    if not isinstance(cursor_secret_name, str) or not cursor_secret_name:
        raise ValueError(f"{path} must define CURSOR_SECRET_NAME")
    return _PoolConfig(
        path=path,
        spec=PoolSpec(pool=pool, worker=worker),
        cursor_secret_name=cursor_secret_name,
    )


_settings = RuntimeSettings()


def _load_configs(paths: tuple[Path, ...]) -> tuple[_PoolConfig, ...]:
    if not paths:
        raise ValueError(f"no pool files found; set {POOL_FILES_ENV} or create pools/*.py")
    configs = tuple(_load_config(path) for path in paths)
    if len({config.cursor_secret_name for config in configs}) != 1:
        raise ValueError("all pool files must use the same CURSOR_SECRET_NAME")
    return configs


_PATHS = _configured_paths()
# Modal hydrates the function module before local-file mounts are visible. The
# deployed function has the exact local image/configuration; load those bundled
# files at invocation time when the mount is available.
_CONFIGS = _load_configs(_PATHS) if _PATHS else ()

_SPECS = tuple(config.spec for config in _CONFIGS)
app = modal.App(CONTROL_PLANE_APP_NAME, tags={"service": "modal-cursor"})
if _CONFIGS:
    _cursor_secret = modal.Secret.from_name(
        _CONFIGS[0].cursor_secret_name, required_keys=["CURSOR_API_KEY"]
    )
    _function_secrets = [_cursor_secret]
    _controller_image = _SPECS[0].pool.control_plane_image()
    for _config in _CONFIGS:
        _controller_image = _controller_image.add_local_file(
            _config.path,
            str(_BUNDLED_POOL_DIR / _config.path.name),
            copy=True,
        )
else:
    _function_secrets = []
    _controller_image = Pool(name="control-plane").control_plane_image()


@app.function(  # pyright: ignore[reportUnknownMemberType]
    image=_controller_image,
    secrets=_function_secrets,
    max_containers=1,
    retries=modal.Retries(max_retries=_settings.controller_max_retries),
    timeout=_settings.controller_timeout_s,
)
def controller() -> None:
    """Own registration, discovery, claiming, and sandbox dispatch for all pools."""
    specs = _SPECS or tuple(config.spec for config in _load_configs(_bundled_paths()))
    run_control_plane(app, specs)
