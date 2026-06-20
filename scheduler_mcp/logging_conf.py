"""Logging structure JSON sur stdout, sans dependance externe."""

import json
import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


class _Logger:
    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    def _emit(self, level: int, event: str, **fields) -> None:
        self._log.log(level, json.dumps({"event": event, **fields}, ensure_ascii=False))

    def debug(self, event, **f):
        self._emit(logging.DEBUG, event, **f)

    def info(self, event, **f):
        self._emit(logging.INFO, event, **f)

    def warning(self, event, **f):
        self._emit(logging.WARNING, event, **f)

    def error(self, event, **f):
        self._emit(logging.ERROR, event, **f)


def get_logger(name: str) -> _Logger:
    return _Logger(name)
