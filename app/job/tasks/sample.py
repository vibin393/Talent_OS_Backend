from celery import shared_task

from app import logger


@shared_task(name="ping")  # type: ignore
def monitor_target() -> bool:
    logger.info("Monitoring....")
    return True
