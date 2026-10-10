from loguru import logger

_configured = False


def setup_logger(with_debug: bool = False) -> None:
    """Add the app file sink once per process (``create_app`` may be called repeatedly, e.g. in tests)."""
    global _configured
    if _configured:
        return
    log_path = "data/logs"
    logger.add(
        f"{log_path}/app.log", rotation="1 hour", compression="zip", level="DEBUG" if with_debug else "INFO"
    )
    _configured = True
