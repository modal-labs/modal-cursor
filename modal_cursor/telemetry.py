"""Best-effort Logfire instrumentation for modal-cursor lifecycle operations."""

from __future__ import annotations

import os
import warnings
from collections.abc import Callable, Generator, Mapping, MutableMapping
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar, cast

import httpx
import logfire
from opentelemetry import context, propagate, trace
from opentelemetry.context import Context

_P = ParamSpec("_P")
_R = TypeVar("_R")


class _TelemetryState:
    configured = False
    disabled = False


_state = _TelemetryState()


def _hide_adapter_frames() -> None:
    """Keep Logfire source locations on lifecycle call sites, not this adapter."""
    try:
        logfire.add_non_user_code_prefix(Path(__file__))
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return


def _logfire_is_configured() -> bool:
    """Avoid replacing an application's explicit Logfire configuration."""
    instance = getattr(logfire, "DEFAULT_LOGFIRE_INSTANCE", None)
    if instance is None:
        return False
    # Logfire's public config object is populated before its provider is live
    # when the pytest integration is active. The provider check avoids treating
    # that intermediate state as an explicit application configuration.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        proxy = getattr(instance, "_tracer_provider", None)
    provider = getattr(proxy, "provider", None)
    return provider is not None and provider.__class__.__name__ != "NoOpTracerProvider"


def configure_telemetry() -> None:
    """Configure quiet, token-gated Logfire defaults once per process.

    Applications may call ``logfire.configure`` themselves first; in that case
    the application's configuration is preserved. Without a token, spans use
    the no-op/export-disabled path and do not produce terminal noise.
    """
    if _state.configured or _state.disabled:
        return
    if _logfire_is_configured():
        _hide_adapter_frames()
        _state.configured = True
        return
    try:
        logfire.configure(
            send_to_logfire="if-token-present",
            service_name=os.environ.get("LOGFIRE_SERVICE_NAME", "modal-cursor"),
            console=False,
            inspect_arguments=False,
            distributed_tracing=True,
        )
    except (OSError, RuntimeError, ValueError):
        # Telemetry is deliberately non-critical to pool provisioning.
        _state.disabled = True
    else:
        _hide_adapter_frames()
        _state.configured = True


@contextmanager
def span(name: str, /, **attributes: object) -> Generator[Any, None, None]:
    """Create a Logfire span, or a no-op context when Logfire is unavailable."""
    configure_telemetry()
    if _state.disabled:
        yield None
        return
    with logfire.span(name, _span_name=name, **cast(dict[str, Any], attributes)) as current:
        yield current


def set_attribute(current: Any, name: str, value: object) -> None:
    """Set an attribute without making application work depend on telemetry."""
    if current is None:
        return
    try:
        current.set_attribute(name, value)
    except (AttributeError, TypeError, ValueError):
        return


def add_event(current: Any, name: str, attributes: Mapping[str, object] | None = None) -> None:
    """Add a structured lifecycle event without making application work depend on telemetry."""
    if current is None:
        return
    try:
        current.add_event(
            name,
            attributes=None if attributes is None else cast(dict[str, Any], dict(attributes)),
        )
    except (AttributeError, TypeError, ValueError):
        return


def current_span() -> Any:
    """Return the active span for decorator-instrumented operations."""
    return trace.get_current_span()


def record_exception(current: Any, error: BaseException) -> None:
    """Mark a handled exception on its enclosing span when possible."""
    if current is None:
        return
    try:
        current.record_exception(error)
        current.set_level("error")
    except (AttributeError, TypeError, ValueError):
        return


def inject_trace_context(carrier: MutableMapping[str, str]) -> None:
    """Inject the active W3C trace context into a subprocess/remote-call carrier."""
    try:
        propagate.inject(carrier)
    except (AttributeError, TypeError, ValueError):
        return


@contextmanager
def continue_trace(carrier: Mapping[str, str]) -> Generator[None, None, None]:
    """Make a propagated W3C context the parent for work in another process."""
    try:
        parent: Context = propagate.extract(carrier)
        token = context.attach(parent)
    except (AttributeError, TypeError, ValueError):
        yield
        return
    try:
        yield
    finally:
        context.detach(token)


def flush_telemetry(timeout_millis: int = 10_000) -> bool:
    """Flush spans before a short-lived bridge or Modal invocation exits."""
    configure_telemetry()
    if _state.disabled:
        return False
    try:
        return bool(logfire.force_flush(timeout_millis=timeout_millis))
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False


@contextmanager
def flush_at_exit(timeout_millis: int = 10_000) -> Generator[None, None, None]:
    """Flush after nested spans close, preserving telemetry for short-lived calls."""
    try:
        yield
    finally:
        flush_telemetry(timeout_millis)


def instrument_httpx(client: httpx.Client) -> None:
    """Instrument one HTTPX client without making the dependency operationally strict."""
    configure_telemetry()
    if _state.disabled:
        return
    try:
        logfire.instrument_httpx(client)  # pyright: ignore[reportUnknownMemberType]
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return


def instrument(name: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Decorate a function with a low-cardinality span without recording arguments."""

    def decorator(function: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(function)
        def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with span(name):
                return function(*args, **kwargs)

        return cast(Callable[_P, _R], wrapped)

    return decorator
