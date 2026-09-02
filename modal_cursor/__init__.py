"""Modal-backed controller for Cursor Self-Hosted Machines worker pools."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modal_cursor.pool import Pool
    from modal_cursor.pools import ConfigError

try:
    __version__ = version("modal-cursor")
except PackageNotFoundError:
    __version__ = "0+unknown"

__all__ = ["ConfigError", "Pool", "__version__"]


def __getattr__(name: str) -> Any:
    """Load the Modal-facing API only when callers ask for it."""
    if name == "Pool":
        from modal_cursor.pool import Pool

        return Pool
    if name == "ConfigError":
        from modal_cursor.pools import ConfigError

        return ConfigError
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
