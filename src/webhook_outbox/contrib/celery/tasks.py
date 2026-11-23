"""
Celery tasks wrapping the Celery-free runners.

Same behaviour as the original tasks (bind=True, max_retries=3, retry delays
60s / 30s / 10s); only the task names changed to the ``webhook_outbox.``
prefix.
"""

import logging

from celery import shared_task

from webhook_outbox import runner
from webhook_outbox.services.delivery import deliver_single

from .schedule import DELIVER_TASK, POLL_TASK, SEND_SINGLE_TASK

logger = logging.getLogger(__name__)


@shared_task(name=POLL_TASK, bind=True, max_retries=3, default_retry_delay=60)
def poll_tracked_tables(self):
    """
    Poll all tracked tables for changes.
    Schedule it periodically with Celery beat (POLL_INTERVAL, 30s).
    """
    try:
        queued_count = runner.poll_once()
        return {"queued": queued_count}
    except Exception as e:
        logger.exception(f"Error polling tracked tables: {e}")
        raise self.retry(exc=e) from e


@shared_task(name=DELIVER_TASK, bind=True, max_retries=3, default_retry_delay=30)
def process_outbox(self):
    """
    Process pending notifications in the outbox.
    Schedule it periodically with Celery beat (SEND_INTERVAL, 10s).
    """
    return runner.deliver_once()


@shared_task(name=SEND_SINGLE_TASK, bind=True, max_retries=3, default_retry_delay=10)
def send_single_notification(self, outbox_id: int):
    """
    Send a single notification immediately.
    A failed attempt that leaves the entry PENDING is retried by Celery.
    """
    result, retryable = deliver_single(outbox_id)
    if retryable:
        raise self.retry(exc=Exception(result["error"]))
    return result


def enqueue_single_notification(outbox_id: int) -> None:
    """
    ADMIN_RETRY_HOOK target: push a retried entry to Celery right away::

        WEBHOOK_OUTBOX = {
            "ADMIN_RETRY_HOOK": (
                "webhook_outbox.contrib.celery.tasks.enqueue_single_notification"
            ),
        }
    """
    send_single_notification.delay(outbox_id)
