"""JSON logs. Every line carries the job_id of the request being worked on, so one id traces a whole request.

Usage:  log.info("hubspot call", extra={"step": "check_crm", "status_code": 200, "duration_ms": 87})
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime

job_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("job_id", default=None)
step_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("step", default=None)

# Attributes every LogRecord has. Anything else on a record was passed through `extra=` and is logged as a field.
_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "job_id": job_id_var.get(),
            "step": step_var.get(),
        }
        line.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD})
        if record.exc_info:
            line["error"] = self.formatException(record.exc_info)
        return json.dumps({k: v for k, v in line.items() if v is not None}, default=str)


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # httpx logs every request with its full URL. Our own "api call" line already covers each call, and some
    # URLs (the Clay webhook) work like secrets, so httpx stays quiet unless something is wrong.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)


@contextmanager
def timed_step(log: logging.Logger, step: str):
    """Tag every log line inside the block with `step`, and log how long the block took."""
    token = step_var.set(step)
    started = time.monotonic()
    try:
        yield
    except Exception as e:
        log.warning("step failed", extra={"duration_ms": _ms(started), "error_type": type(e).__name__})
        raise
    else:
        log.info("step done", extra={"duration_ms": _ms(started)})
    finally:
        step_var.reset(token)


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
