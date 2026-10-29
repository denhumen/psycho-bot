import logging
import re
import sys

# chat_id у текстах логів aiogram/власних не пишемо; токен — маскуємо про всяк випадок.
_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _TOKEN_RE.sub("<token>", message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_RedactFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # aiogram.event пише рядок на кожен апдейт — зайве; помилки все одно лишаються.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
