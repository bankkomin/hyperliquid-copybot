"""structlog -> logs/copybot.jsonl.

Must not touch stdout: start_copybot.bat launches us with pythonw.exe, where
sys.stdout and sys.stderr are None and structlog's default PrintLogger raises on
the first log call.
"""

import logging
import logging.handlers
from pathlib import Path

import structlog

LOG_PATH = Path("logs/copybot.jsonl")


def configure(log_path: Path = LOG_PATH) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.TimedRotatingFileHandler(
        log_path, when="midnight", backupCount=14, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    # Flask/Dash/werkzeug log plain sentences through the same root logger. Drop
    # anything that is not a JSON object so the file stays parseable as JSONL.
    handler.addFilter(lambda r: r.getMessage().startswith("{"))
    root = logging.getLogger()
    root.handlers = [handler]  # never a StreamHandler: stdout is None under pythonw
    root.setLevel(logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
