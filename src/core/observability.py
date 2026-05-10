"""Langfuse tracer wrapper.

Centralizes Langfuse initialization so the rest of the code stays
unaware of whether observability is enabled. Returns no-op shims when
Langfuse keys are absent — useful for local dev and CI.
"""

from contextlib import contextmanager
from typing import Any

from src.core.config import get_settings
from src.core.logging import get_logger

logger = get_logger(__name__)

_client: Any = None


def get_tracer() -> Any:
    """Return a Langfuse client, or a no-op shim if disabled."""
    global _client
    if _client is not None:
        return _client

    settings = get_settings()
    if not settings.langfuse_enabled:
        logger.info("langfuse.disabled")
        _client = _NoopTracer()
        return _client

    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        logger.info("langfuse.enabled", host=settings.langfuse_host)
    except Exception as exc:  # noqa: BLE001 — fall back on any init error
        logger.warning("langfuse.init_failed", error=str(exc))
        _client = _NoopTracer()
    return _client


class _NoopTracer:
    """Drop-in replacement when Langfuse is not configured."""

    def trace(self, **_: Any) -> "_NoopSpan":
        return _NoopSpan()

    def flush(self) -> None:
        pass


class _NoopSpan:
    def span(self, **_: Any) -> "_NoopSpan":
        return self

    def update(self, **_: Any) -> None:
        pass

    def end(self, **_: Any) -> None:
        pass

    def generation(self, **_: Any) -> "_NoopSpan":
        return self


@contextmanager
def trace_run(name: str, **metadata: Any):
    """Context manager that creates a Langfuse trace for one full agent run."""
    tracer = get_tracer()
    trace = tracer.trace(name=name, metadata=metadata)
    try:
        yield trace
    finally:
        tracer.flush()
