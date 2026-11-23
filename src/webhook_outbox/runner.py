"""
Celery-free runners.

``poll_once`` / ``deliver_once`` / ``deliver_entry`` run one unit of work and
return; ``run_forever`` is a small worker loop that calls them on two
intervals until it is stopped (SIGTERM / SIGINT or a ``threading.Event``).

The management commands ``webhook_outbox_worker``, ``webhook_outbox_poll``
and ``webhook_outbox_deliver`` are thin wrappers around these functions.
"""

import logging
import signal
import threading
import time
from collections.abc import Callable
from typing import Any

from django.db import close_old_connections

from webhook_outbox.conf import get_setting
from webhook_outbox.services.delivery import process_outbox, send_single_notification
from webhook_outbox.services.poller import poll_all_tables

logger = logging.getLogger(__name__)

# How often (seconds) the loop looks at the "got a signal" flag while idle.
SIGNAL_CHECK_INTERVAL = 0.5


def poll_once() -> int:
    """Poll every active tracked table once. Returns the number of entries queued."""
    queued_count = poll_all_tables()
    # runs every POLL_INTERVAL (30s) and is a no-op most of the time — only
    # say something when there was actually work
    if queued_count:
        logger.info(f"Polling complete. Queued {queued_count} notifications.")
    return queued_count


def deliver_once(limit: int | None = None) -> dict[str, int]:
    """
    Run one delivery pass over pending entries (at most ``limit``, default
    DELIVERY_BATCH_SIZE). Returns ``{"sent": n, "failed": n}``.
    """
    return process_outbox(limit=limit)


def deliver_entry(pk: int) -> dict[str, Any]:
    """Send one outbox entry right now (e.g. as ADMIN_RETRY_HOOK)."""
    return send_single_notification(pk)


def _run_step(name: str, func: Callable[[], Any]) -> None:
    close_old_connections()
    try:
        func()
    except Exception:
        # The loop must survive a broken database connection, a bad channel
        # class and the like; the next tick tries again.
        logger.exception(f"webhook_outbox worker: {name} failed")
    finally:
        close_old_connections()


def run_forever(
    poll_interval: float | None = None,
    send_interval: float | None = None,
    stop_event: threading.Event | None = None,
    *,
    poll: bool = True,
    deliver: bool = True,
    install_signal_handlers: bool = True,
) -> None:
    """
    Poll every ``poll_interval`` seconds and deliver every ``send_interval``
    seconds (defaults: POLL_INTERVAL = 30, SEND_INTERVAL = 10) until
    ``stop_event`` is set.

    When called from the main thread with ``install_signal_handlers`` (the
    default), SIGTERM and SIGINT set ``stop_event``: the step in progress
    finishes, then the loop returns. The previous handlers are restored on
    exit.

    Run one worker per database: the poller does not lock tracked tables,
    so two loops polling at once can queue the same change twice outside
    the dedup window (see docs/semantics.md).
    """
    if poll_interval is None:
        poll_interval = get_setting("POLL_INTERVAL")
    if send_interval is None:
        send_interval = get_setting("SEND_INTERVAL")
    if poll_interval <= 0 or send_interval <= 0:
        raise ValueError("poll_interval and send_interval must be positive")
    if not (poll or deliver):
        raise ValueError("Nothing to run: both poll and deliver are disabled")

    stop = stop_event if stop_event is not None else threading.Event()

    # The signal handler only records the signal: Event.set() takes a
    # non-reentrant lock that the interrupted main thread may be holding.
    # The loop polls this flag between short wait slices.
    signalled: list[int] = []

    previous_handlers: dict[int, Any] = {}
    if (
        install_signal_handlers
        and threading.current_thread() is threading.main_thread()
    ):

        def _handle_signal(signum, frame):
            logger.info(
                f"webhook_outbox worker: got {signal.Signals(signum).name}, stopping"
            )
            signalled.append(signum)

        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, _handle_signal)

    logger.info(
        "webhook_outbox worker started "
        f"(poll={'every %ss' % poll_interval if poll else 'off'}, "
        f"deliver={'every %ss' % send_interval if deliver else 'off'})"
    )

    now = time.monotonic()
    next_poll = now if poll else float("inf")
    next_send = now if deliver else float("inf")

    try:
        while not (stop.is_set() or signalled):
            now = time.monotonic()
            if now >= next_poll:
                _run_step("poll", poll_once)
                next_poll = now + poll_interval
            if stop.is_set() or signalled:
                break
            now = time.monotonic()
            if now >= next_send:
                _run_step("deliver", deliver_once)
                next_send = now + send_interval
            timeout = max(0.0, min(next_poll, next_send) - time.monotonic())
            stop.wait(min(timeout, SIGNAL_CHECK_INTERVAL))
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        logger.info("webhook_outbox worker stopped")
