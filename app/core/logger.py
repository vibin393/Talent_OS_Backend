import logging

from loguru import logger as _log


def setup_logger(with_debug: bool = False) -> None:
    log_path = "data/logs"

    # App Info Log
    _log.add(
        f"{log_path}/app.log",
        rotation="1 day",
        compression="zip",
        filter=lambda record: record["extra"].get("context") not in {"system", "comm", "trade"},
        level="INFO",
    )

    # App Error logs
    _log.add(
        f"{log_path}/error.log",
        rotation="1 day",
        compression="zip",
        filter=lambda record: record["extra"].get("context") not in {"system", "comm", "trade"}
        and record["level"].name in {"ERROR", "CRITICAL"},
        level="DEBUG" if with_debug else "ERROR",
    )

    # System logs (system)
    _log.add(
        f"{log_path}/system.log",
        rotation="1 day",
        compression="zip",
        level="DEBUG" if with_debug else "INFO",
        filter=lambda record: record["extra"].get("context") == "system",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {module}:{function}:{line} | {message}",
    )

    # Communication logs (communication)
    _log.add(
        f"{log_path}/comm.log",
        rotation="1 day",
        compression="zip",
        level="DEBUG" if with_debug else "INFO",
        filter=lambda record: record["extra"].get("context") == "comm",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {module}:{function}:{line} | {message}",
    )

    # Background Job logs (communication)
    _log.add(
        f"{log_path}/job.log",
        rotation="1 day",
        compression="zip",
        level="DEBUG" if with_debug else "INFO",
        filter=lambda record: record["extra"].get("context") == "job",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {module}:{function}:{line} | {message}",
    )

    # Background Job logs (communication)
    _log.add(
        f"{log_path}/external.log",
        rotation="1 day",
        compression="zip",
        level="DEBUG" if with_debug else "INFO",
        filter=lambda record: record["extra"].get("context") == "external",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {module}:{function}:{line} | {message}",
    )


# slogger is for System Logger
slogger = _log.bind(context="system")

# clogger is for Communication Logger
clogger = _log.bind(context="comm")

# clogger is for Communication Logger
jlogger = _log.bind(context="job")

# clogger is for Communication Logger
elogger = _log.bind(context="external")

# Generic Logger
logger = _log


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        logger_opt = logger.opt(depth=6, exception=record.exc_info if record.exc_info else None)
        logger_opt.log(record.levelname, record.getMessage())


logging.basicConfig(handlers=[InterceptHandler()], level=logging.INFO)


# ================================================================ secret redaction (Epic 12 A10)
import re  # noqa: E402

REDACTED = "[redacted]"
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # query/body style ``token=...`` (invitation / reset links, OAuth callbacks)
    (re.compile(r"(?i)(\btoken=)[^&\s\"'#]+"), r"\1" + REDACTED),
    # Story 22.9 (A10): portal fragment tokens ``#t=...`` (Client magic links, Vendor invite / reset links)
    (re.compile(r"(#t=)[^&\s\"']+"), r"\1" + REDACTED),
    # Epic 7 OAuth callbacks ``/connections/<provider>/callback?code=...&state=...``
    (re.compile(r"(?i)([?&](?:code|state)=)[^&\s\"'#]+"), r"\1" + REDACTED),
    # Epic 6 secure links ``/l/<token>`` and the public API ``/public/links/<token>``
    (re.compile(r"(/l/)[A-Za-z0-9_\-]{8,}"), r"\1" + REDACTED),
    (re.compile(r"(/public/links/)[A-Za-z0-9_\-]{8,}"), r"\1" + REDACTED),
    # legacy path-token invitation endpoints ``/auth/invitations/<token>``
    (re.compile(r"(/auth/invitations/)(?!lookup\b|accept\b)[A-Za-z0-9_.\-]{16,}"), r"\1" + REDACTED),
    # ``Authorization: Bearer ...`` and raw API keys
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_.\-]+"), r"\1" + REDACTED),
    (re.compile(r"\btos_[0-9a-f]{8}_[A-Za-z0-9_\-]{20,}"), "tos_" + REDACTED),
    # Epic 66 (NFR-63): OAuth access / refresh tokens, auth codes and App client secrets
    (re.compile(r"\btos_(at|rt|ac|cs)_[A-Za-z0-9_\-]+"), r"tos_\1_" + REDACTED),
    # Epic 73: MCP session ids (``Mcp-Session-Id: mcps_...``)
    (re.compile(r"\bmcps_[A-Za-z0-9_\-]+"), "mcps_" + REDACTED),
)

# Epic 66 (NFR-63): secret values read through the vault in this request / task are masked in every log line.
from contextvars import ContextVar  # noqa: E402

_MIN_SECRET_LEN = 6
_MAX_SECRET_VALUES = 64
_secret_values: ContextVar[tuple[str, ...]] = ContextVar("talent_os_log_secret_values", default=())


def register_secret_value(value: str | None) -> None:
    """Mask ``value`` in log lines emitted later in this context (``SecretBag.get`` registers what it
    returns). Values under 6 characters are ignored (ordinary words stay readable); at most 64 are kept."""
    if not isinstance(value, str) or len(value) < _MIN_SECRET_LEN:
        return
    current = _secret_values.get()
    if value in current:
        return
    # longest first, so a value containing another one is masked whole
    _secret_values.set(tuple(sorted((value, *current), key=len, reverse=True))[:_MAX_SECRET_VALUES])


def clear_secret_values() -> None:
    _secret_values.set(())


def redact(text: str) -> str:
    """Mask tokens, secure-link paths and bearer credentials in a log line."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    for value in _secret_values.get():
        if value in text:
            text = text.replace(value, REDACTED)
    # Epic 21 (NFR-24): a full Aadhaar number never reaches a log line (``XXXX-XXXX-1234``).
    from app.core.identity_docs import mask_full_aadhaar

    return mask_full_aadhaar(text)


def _patch(record: "Any") -> None:
    record["message"] = redact(record["message"])


def _redact_arg(value: object) -> object:
    return redact(value) if isinstance(value, str) else value


class RedactingFilter(logging.Filter):
    """Standard-library filter (uvicorn access log) applying ``redact`` to the message and its arguments.

    The shape of ``record.args`` is preserved: uvicorn's ``AccessFormatter`` unpacks exactly five args
    (client, method, path, http version, status), so collapsing them into a pre-formatted ``msg`` with
    ``args=()`` broke every access line that carried a token (``ValueError: not enough values to unpack``).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            args = record.args
            if isinstance(args, tuple):
                record.args = tuple(_redact_arg(a) for a in args)
            elif isinstance(args, dict):
                record.args = {k: _redact_arg(v) for k, v in args.items()}
        except Exception:  # never drop a log line because of redaction
            return True
        return True


def install_redaction() -> None:
    _log.configure(patcher=_patch)
    for name in ("", "uvicorn.access", "uvicorn.error", "httpx"):
        target = logging.getLogger(name)
        if not any(isinstance(f, RedactingFilter) for f in target.filters):
            target.addFilter(RedactingFilter())


from typing import Any  # noqa: E402

install_redaction()
