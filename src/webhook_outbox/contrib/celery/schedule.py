"""
Celery beat schedule helper.

Safe to import from a Django settings module: it imports no models and does
not read Django settings.
"""

from webhook_outbox.conf import DEFAULTS

POLL_TASK = "webhook_outbox.poll_tracked_tables"
DELIVER_TASK = "webhook_outbox.process_outbox"
SEND_SINGLE_TASK = "webhook_outbox.send_single_notification"


def beat_schedule(
    poll_interval: float | None = None, send_interval: float | None = None
) -> dict:
    """
    Entries for ``CELERY_BEAT_SCHEDULE``::

        from webhook_outbox.contrib.celery.schedule import beat_schedule

        CELERY_BEAT_SCHEDULE = {**beat_schedule(), ...}

    Defaults: poll every 30s, deliver every 10s (the POLL_INTERVAL /
    SEND_INTERVAL defaults). Pass the same values you put in WEBHOOK_OUTBOX
    if you changed them.
    """
    if poll_interval is None:
        poll_interval = DEFAULTS["POLL_INTERVAL"]
    if send_interval is None:
        send_interval = DEFAULTS["SEND_INTERVAL"]
    return {
        "webhook-outbox-poll-tracked-tables": {
            "task": POLL_TASK,
            "schedule": float(poll_interval),
        },
        "webhook-outbox-process-outbox": {
            "task": DELIVER_TASK,
            "schedule": float(send_interval),
        },
    }
