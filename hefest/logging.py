from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from hefest.config import Settings

_INTERCEPT_LOGGERS: tuple[str, ...] = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "fastapi",
    "tortoise",
)

_DEV_FORMAT = (
    "<green>{time:HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — "
    "<level>{message}</level>"
)
_PROD_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{name}:{function}:{line} | {message}"
)


class _InterceptHandler(logging.Handler):
    """Redirect stdlib logging records to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def configure_logging(settings: Settings) -> None:
    """Configure loguru sinks for the current environment.

    Dev: coloured stderr only.
    Non-dev: plain stderr + daily-rotating file sink with gz compression
    and 30-day retention.
    """
    logger.remove()

    is_dev = settings.env == "dev"

    logger.add(
        sys.stderr,
        level=settings.log_level,
        colorize=is_dev,
        format=_DEV_FORMAT if is_dev else _PROD_FORMAT,
    )

    if not is_dev:
        logger.add(
            "logs/hefest.log",
            level=settings.log_level,
            format=_PROD_FORMAT,
            rotation="00:00",
            retention="30 days",
            compression="gz",
            enqueue=True,
            encoding="utf-8",
        )

    intercept = _InterceptHandler()
    logging.basicConfig(handlers=[intercept], level=0, force=True)
    for name in _INTERCEPT_LOGGERS:
        log = logging.getLogger(name)
        log.handlers = [intercept]
        log.propagate = False
