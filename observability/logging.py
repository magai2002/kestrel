"""structlog JSON to stdout (spec Section 9), collected by Docker. Bind
``run_id``, ``sleeve``, ``mode`` and ``client_order_id`` where relevant
(``structlog.contextvars.bind_contextvars``)."""

from __future__ import annotations

import logging
import sys

import structlog


def configure(level: str = "INFO", json: bool = True) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    # Per-request INFO lines from HTTP/IB libraries drown the cycle logs.
    for noisy in ("httpx", "httpcore", "ib_async"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
