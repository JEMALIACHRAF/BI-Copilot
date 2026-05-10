"""Structured JSON logging.

Emits one JSON object per log line so we can query traces in Cloud Logging
or any log aggregator without regex parsing.
"""

import logging
import sys

import structlog

from src.core.config import get_settings


def configure_logging() -> None:
    """Configure structlog with JSON output, ISO timestamps, and stdlib bridge."""
    settings = get_settings()
    level = getattr(logging, settings.log_level)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog-bound logger for the given module."""
    return structlog.get_logger(name)
