"""
Delivery of outbox entries, without Celery.

``process_outbox`` and ``send_single_notification`` are the bodies of the
Celery tasks of the same name in the original implementation, moved into
plain functions so they can run from a management command, a plain worker
loop or any task queue. ``webhook_outbox.contrib.celery`` wraps them back
into Celery tasks.
"""

import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from webhook_outbox.conf import get_setting
from webhook_outbox.models import NotificationOutbox
from webhook_outbox.services.channels import dispatch

logger = logging.getLogger(__name__)


def send_notification(entry: NotificationOutbox) -> tuple[bool, str]:
    """
    Send a notification through the appropriate channel.

    Args:
        entry: NotificationOutbox entry to send

    Returns:
        Tuple of (success, error_message)
    """
    return dispatch(entry)


def _pending_entries(max_retries: int, limit: int):
    return (
        NotificationOutbox.objects.filter(
            status=NotificationOutbox.Status.PENDING,
            retry_count__lt=max_retries,
        )
        .select_related(
            "tracked_table",
            "tracked_table__channel",
        )
        .order_by("created_at")[:limit]
    )


def process_outbox(limit: int | None = None) -> dict[str, int]:
    """
    Process pending notifications in the outbox.

    Takes up to ``limit`` (default DELIVERY_BATCH_SIZE = 100) PENDING entries
    with ``retry_count < MAX_RETRIES``, oldest ``created_at`` first, sends
    each one and records the result:

    * success: status SENT, ``sent_at`` = now, ``error_message`` cleared;
    * failure: ``retry_count`` += 1, ``error_message`` = the channel error,
      status FAILED once ``retry_count`` reaches MAX_RETRIES (else it stays
      PENDING for the next pass).

    Returns:
        {"sent": <entries sent>, "failed": <entries that became FAILED or errored>}
    """
    max_retries = get_setting("MAX_RETRIES")
    if limit is None:
        limit = get_setting("DELIVERY_BATCH_SIZE")

    pending = _pending_entries(max_retries, limit)

    sent_count = 0
    failed_count = 0

    for entry in pending:
        try:
            success, error = send_notification(entry)

            with transaction.atomic():
                entry = NotificationOutbox.objects.select_for_update().get(pk=entry.pk)

                if success:
                    entry.status = NotificationOutbox.Status.SENT
                    entry.sent_at = timezone.now()
                    entry.error_message = ""
                    sent_count += 1
                else:
                    entry.retry_count += 1
                    entry.error_message = error

                    if entry.retry_count >= max_retries:
                        entry.status = NotificationOutbox.Status.FAILED
                        failed_count += 1

                entry.save()

        except Exception as e:
            logger.exception(f"Error processing outbox entry #{entry.pk}: {e}")
            failed_count += 1

    # runs every SEND_INTERVAL (10s); stay quiet on an empty outbox
    if sent_count or failed_count:
        logger.info(
            f"Outbox processing complete. Sent: {sent_count}, Failed: {failed_count}"
        )
    return {"sent": sent_count, "failed": failed_count}


def deliver_single(outbox_id: int) -> tuple[dict[str, Any], bool]:
    """
    Send one outbox entry now, whatever its retry count (SENT entries are
    skipped), and record the result like ``process_outbox`` does.

    Returns:
        (result, retryable): ``result`` is one of
        ``{"error": "Entry not found"}``, ``{"status": "already_sent"}`` or
        ``{"success": bool, "error": str}``; ``retryable`` is True when the
        attempt failed and the entry is still PENDING (a task queue may
        schedule another attempt).
    """
    max_retries = get_setting("MAX_RETRIES")

    try:
        entry = NotificationOutbox.objects.select_related(
            "tracked_table",
            "tracked_table__channel",
        ).get(pk=outbox_id)
    except NotificationOutbox.DoesNotExist:
        logger.error(f"Outbox entry #{outbox_id} not found")
        return {"error": "Entry not found"}, False

    if entry.status == NotificationOutbox.Status.SENT:
        logger.warning(f"Outbox entry #{outbox_id} already sent")
        return {"status": "already_sent"}, False

    success, error = send_notification(entry)
    retryable = False

    with transaction.atomic():
        entry = NotificationOutbox.objects.select_for_update().get(pk=entry.pk)

        if success:
            entry.status = NotificationOutbox.Status.SENT
            entry.sent_at = timezone.now()
            entry.error_message = ""
        else:
            entry.retry_count += 1
            entry.error_message = error

            if entry.retry_count >= max_retries:
                entry.status = NotificationOutbox.Status.FAILED
            else:
                retryable = True

        entry.save()

    return {"success": success, "error": error}, retryable


def send_single_notification(outbox_id: int) -> dict[str, Any]:
    """Send one outbox entry immediately; see ``deliver_single``."""
    result, _retryable = deliver_single(outbox_id)
    return result
