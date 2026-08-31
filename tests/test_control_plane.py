from __future__ import annotations

import runpy
from pathlib import Path

import pytest

from modal_cursor import cli, control_plane


def test_control_plane_loads_pool_files_and_builds_one_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli, "_secret_names", lambda: {"cursor-service-account"})
    cli.init_pool(name="gpu", pools_dir=tmp_path)
    pool_file = tmp_path / "gpu.py"

    monkeypatch.setenv(control_plane.POOL_FILES_ENV, str(pool_file))
    namespace = runpy.run_path(str(Path(control_plane.__file__)))

    assert namespace["_PATHS"] == (pool_file.resolve(),)
    assert len(namespace["_CONFIGS"]) == 1
    assert namespace["_CONFIGS"][0].cursor_secret_name == "cursor-service-account"
    assert namespace["app"].name == control_plane.CONTROL_PLANE_APP_NAME
    assert set(namespace["app"].registered_functions) == {"controller"}


def test_control_plane_can_import_before_pool_files_are_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(control_plane.POOL_FILES_ENV, raising=False)
    monkeypatch.setattr(control_plane, "_BUNDLED_POOL_DIR", tmp_path / "not-mounted")
    monkeypatch.chdir(tmp_path)

    namespace = runpy.run_path(str(Path(control_plane.__file__)))

    assert namespace["_PATHS"] == ()
    assert namespace["_CONFIGS"] == ()
    assert namespace["_function_secrets"] == []
    assert set(namespace["app"].registered_functions) == {"controller"}
