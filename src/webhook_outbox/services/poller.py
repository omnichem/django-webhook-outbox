import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import models, transaction
from django.db.models import F, Q
from django.db.utils import IntegrityError
from django.utils import timezone

from webhook_outbox.conf import get_setting
from webhook_outbox.models import NotificationOutbox, TrackedTable
from webhook_outbox.services.template import render_template

logger = logging.getLogger(__name__)

POLLED_EVENT_TYPES = (TrackedTable.EventType.INSERT, TrackedTable.EventType.UPDATE)

# auto_now_add and auto_now each read the clock, so a new row has updated_at a
# few microseconds after created_at. With a trailing cursor, a row created
# after the cursor only counts as *updated* when updated_at is clearly later
# than created_at.
CREATION_TOLERANCE = timedelta(seconds=1)


@dataclass
class DetectedChange:
    """Represents a detected change in a tracked table."""

    instance: models.Model
    event_type: str
    # The row timestamp that made the change visible: created_at for INSERT,
    # updated_at for UPDATE. Stored on the outbox entry for exact dedup.
    changed_at: datetime | None = None


class TablePoller:
    """
    Polls tracked tables for changes based on timestamps.

    Uses created_at for INSERT detection and updated_at for UPDATE detection.
    Assumes models inherit from TimeStampedModel with created_at and updated_at fields.

    The rows are read through the model's default ``objects`` manager, so a
    manager that filters rows (e.g. hides soft-deleted ones) filters events.

    ``poll_table()`` is the entry point that writes outbox entries; it uses
    ``detect()`` and ``next_cursor()`` inside one transaction.
    """

    def __init__(self, tracked_table: TrackedTable):
        self.tracked_table = tracked_table
        self.model_class = tracked_table.get_model_class()

    def _has_required_fields(self) -> bool:
        """Check if the model has the required timestamp fields."""
        if self.model_class is None:
            return False

        meta = self.model_class._meta
        field_names = {f.name for f in meta.get_fields()}

        if self.tracked_table.event_type == TrackedTable.EventType.INSERT:
            return "created_at" in field_names
        elif self.tracked_table.event_type == TrackedTable.EventType.UPDATE:
            return "created_at" in field_names and "updated_at" in field_names

        return False

    def is_polled(self) -> bool:
        """DELETE rules are fed by signal capture / enqueue(), never polled."""
        return self.tracked_table.event_type in POLLED_EVENT_TYPES

    def can_poll(self) -> bool:
        """Log and return False when the tracked model cannot be polled."""
        if not self.is_polled():
            return False

        if self.model_class is None:
            logger.error(
                f"Model not found: {self.tracked_table.app_label}.{self.tracked_table.model_name}"
            )
            return False

        if not self._has_required_fields():
            logger.error(
                f"Model {self.tracked_table.model_path} missing required timestamp fields"
            )
            return False

        return True

    def detect(self, since) -> list[DetectedChange]:
        """Rows changed after ``since``. No side effects."""
        if self.tracked_table.event_type == TrackedTable.EventType.INSERT:
            return self._detect_inserts(since)
        if self.tracked_table.event_type == TrackedTable.EventType.UPDATE:
            return self._detect_updates(since)
        return []

    @staticmethod
    def next_cursor(since, now):
        """
        The cursor after a successful poll that started at ``now``: it trails
        ``now`` by POLL_OVERLAP_SECONDS (so rows that commit late are read
        again) and never moves backwards.
        """
        if since is None:
            return now
        overlap = timedelta(seconds=get_setting("POLL_OVERLAP_SECONDS"))
        return max(since, now - overlap)

    def poll(self) -> list[DetectedChange]:
        """
        Detect changes since the last poll and move the cursor right away.

        Kept for code that called it in 1.0. It saves the cursor before the
        caller writes the outbox entries, so an error in between loses
        changes: use ``poll_table()``, which writes the entries and the
        cursor in one transaction.

        The first poll of a tracked table only stores the cursor (no
        backfill).

        Returns:
            List of DetectedChange objects
        """
        if not self.can_poll():
            return []

        last_polled = self.tracked_table.last_polled_at
        now = timezone.now()

        if last_polled is None:
            self.tracked_table.last_polled_at = now
            self.tracked_table.save(update_fields=["last_polled_at"])
            logger.info(
                f"First poll for {self.tracked_table.model_path} - "
                f"initialized last_polled_at to {now}, will track new changes only"
            )
            return []

        changes = self.detect(last_polled)

        self.tracked_table.last_polled_at = self.next_cursor(last_polled, now)
        self.tracked_table.save(update_fields=["last_polled_at"])

        return changes

    def _detect_inserts(self, since) -> list[DetectedChange]:
        """Detect new records created since the given timestamp."""
        queryset = self.model_class.objects.filter(created_at__gt=since)

        changes = []
        for instance in queryset:
            changes.append(
                DetectedChange(
                    instance=instance,
                    event_type=TrackedTable.EventType.INSERT,
                    changed_at=instance.created_at,
                )
            )

        if changes:
            logger.info(
                f"Detected {len(changes)} INSERT(s) in {self.tracked_table.model_path}"
            )

        return changes

    def _detect_updates(self, since) -> list[DetectedChange]:
        """
        Detect records updated since the given timestamp.

        Excludes newly created records (where created_at >= since). With
        POLL_OVERLAP_SECONDS > 0 the cursor trails the poll time, so a row
        created after the cursor is also reported when its updated_at is more
        than CREATION_TOLERANCE after its created_at: otherwise an update made
        shortly after a row was created would never be reported.
        """
        condition = Q(created_at__lt=since)
        if get_setting("POLL_OVERLAP_SECONDS") > 0:
            condition |= Q(updated_at__gt=F("created_at") + CREATION_TOLERANCE)
        queryset = self.model_class.objects.filter(
            condition,
            updated_at__gt=since,
        )

        changes = []
        for instance in queryset:
            changes.append(
                DetectedChange(
                    instance=instance,
                    event_type=TrackedTable.EventType.UPDATE,
                    changed_at=instance.updated_at,
                )
            )

        if changes:
            logger.info(
                f"Detected {len(changes)} UPDATE(s) in {self.tracked_table.model_path}"
            )

        return changes


def poll_all_tables() -> int:
    """
    Poll all active tracked tables and create outbox entries for detected changes.

    An exception while polling one table is logged and does not stop the
    other tables. DELETE rules are not polled.

    Returns:
        Number of notifications queued
    """
    active_tables = TrackedTable.objects.filter(
        is_active=True,
        channel__is_active=True,
        event_type__in=POLLED_EVENT_TYPES,
    ).select_related("channel")

    total_queued = 0

    for tracked_table in active_tables:
        try:
            queued = poll_table(tracked_table)
            total_queued += queued
        except Exception as e:
            logger.exception(f"Error polling {tracked_table.model_path}: {e}")

    return total_queued


def poll_table(tracked_table: TrackedTable) -> int:
    """
    Poll a single tracked table and create outbox entries.

    Everything happens in one transaction that holds a row lock on the
    tracked table (``SELECT ... FOR UPDATE``), so two pollers of the same
    table run one after the other. The cursor is re-read under the lock.

    * Each change is written in its own savepoint: one row that cannot be
      written does not stop the others.
    * The cursor only moves when every detected change was written (or
      recognised as a duplicate). Otherwise it stays where it was and the
      next poll reads the same window again.
    * The cursor moves to ``max(cursor, now - POLL_OVERLAP_SECONDS)``, so the
      next poll re-reads the overlap and catches rows that committed late.

    A change is skipped when it is already queued: an entry for the same
    (tracked_table, source_pk, event_type) exists with the same source row
    timestamp (exact), or was created within the dedup window
    (``tracked_table.dedup_window_seconds``, else DEDUP_WINDOW_SECONDS, 5
    minutes by default).

    Args:
        tracked_table: TrackedTable configuration. Its ``last_polled_at`` is
            updated in place.

    Returns:
        Number of notifications queued
    """
    poller = TablePoller(tracked_table)
    if not poller.is_polled():
        logger.debug(f"Not polling {tracked_table}: DELETE rules are not polled")
        return 0
    if not poller.can_poll():
        return 0

    with transaction.atomic():
        since = (
            TrackedTable.objects.select_for_update()
            .values_list("last_polled_at", flat=True)
            .get(pk=tracked_table.pk)
        )
        tracked_table.last_polled_at = since
        now = timezone.now()

        if since is None:
            _save_cursor(tracked_table, now)
            logger.info(
                f"First poll for {tracked_table.model_path} - "
                f"initialized last_polled_at to {now}, will track new changes only"
            )
            return 0

        changes = poller.detect(since)
        queued, failed = _queue_changes(tracked_table, changes)

        if failed:
            logger.error(
                f"{failed} change(s) of {tracked_table.model_path} could not be "
                f"queued; the cursor stays at {since} and the next poll retries them"
            )
            return queued

        new_cursor = poller.next_cursor(since, now)
        if new_cursor != since:
            _save_cursor(tracked_table, new_cursor)

    return queued


def _save_cursor(tracked_table: TrackedTable, value) -> None:
    tracked_table.last_polled_at = value
    TrackedTable.objects.filter(pk=tracked_table.pk).update(last_polled_at=value)


def _queue_changes(
    tracked_table: TrackedTable, changes: list[DetectedChange]
) -> tuple[int, int]:
    """Write one outbox entry per new change. Returns (queued, failed)."""
    queued = 0
    failed = 0

    for change in changes:
        try:
            with transaction.atomic():
                if _is_duplicate(tracked_table, change):
                    logger.debug(
                        f"Skipping duplicate notification for "
                        f"{tracked_table.model_path}#{change.instance.pk}"
                    )
                    continue

                message = render_template(
                    tracked_table.message_template,
                    change.instance,
                )

                NotificationOutbox.objects.create(
                    tracked_table=tracked_table,
                    source_model=tracked_table.model_path,
                    source_pk=str(change.instance.pk),
                    event_type=change.event_type,
                    message=message,
                    status=NotificationOutbox.Status.PENDING,
                    source_changed_at=change.changed_at,
                )

            queued += 1
            logger.debug(
                f"Queued notification for {tracked_table.model_path}#{change.instance.pk}"
            )

        except IntegrityError:
            logger.debug(
                f"Duplicate notification prevented for "
                f"{tracked_table.model_path}#{change.instance.pk}"
            )
        except Exception as e:
            failed += 1
            logger.exception(
                f"Error creating outbox entry for "
                f"{tracked_table.model_path}#{change.instance.pk}: {e}"
            )

    return queued, failed


def get_dedup_window(tracked_table: TrackedTable) -> int:
    """The dedup window of a tracked table, in seconds."""
    override = getattr(tracked_table, "dedup_window_seconds", None)
    if override is not None:
        return override
    return get_setting("DEDUP_WINDOW_SECONDS")


def _is_duplicate(tracked_table: TrackedTable, change: DetectedChange) -> bool:
    """
    Check if a notification for this change was already queued.

    * Exact: an entry for the same (tracked_table, source_pk, event_type)
      carries the same source row timestamp. This absorbs the poll overlap,
      re-reads after a failed poll and events already queued by signal
      capture or enqueue().
    * Window: an entry for the same (tracked_table, source_pk, event_type)
      was created within the dedup window (``dedup_window_seconds`` of the
      tracked table, else DEDUP_WINDOW_SECONDS, 300 seconds by default).
      This is the 1.0 rule; it also drops a real second change in the window.
    """
    same_source = NotificationOutbox.objects.filter(
        tracked_table=tracked_table,
        source_pk=str(change.instance.pk),
        event_type=change.event_type,
    )

    if (
        change.changed_at is not None
        and same_source.filter(source_changed_at=change.changed_at).exists()
    ):
        return True

    window = timedelta(seconds=get_dedup_window(tracked_table))
    recent_cutoff = timezone.now() - window

    return same_source.filter(created_at__gte=recent_cutoff).exists()
