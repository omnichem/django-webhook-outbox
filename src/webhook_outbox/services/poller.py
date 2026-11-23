import logging
from dataclasses import dataclass
from datetime import timedelta

from django.db import models, transaction
from django.db.utils import IntegrityError
from django.utils import timezone

from webhook_outbox.conf import get_setting
from webhook_outbox.models import NotificationOutbox, TrackedTable
from webhook_outbox.services.template import render_template

logger = logging.getLogger(__name__)


@dataclass
class DetectedChange:
    """Represents a detected change in a tracked table."""

    instance: models.Model
    event_type: str


class TablePoller:
    """
    Polls tracked tables for changes based on timestamps.

    Uses created_at for INSERT detection and updated_at for UPDATE detection.
    Assumes models inherit from TimeStampedModel with created_at and updated_at fields.

    The rows are read through the model's default ``objects`` manager, so a
    manager that filters rows (e.g. hides soft-deleted ones) filters events.
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

    def poll(self) -> list[DetectedChange]:
        """
        Poll the tracked table for changes since the last poll.

        The first poll of a tracked table only stores the cursor (no
        backfill). The cursor (``last_polled_at``) is advanced to the time
        the poll started, before the caller writes the outbox entries.

        Returns:
            List of DetectedChange objects
        """
        if self.model_class is None:
            logger.error(
                f"Model not found: {self.tracked_table.app_label}.{self.tracked_table.model_name}"
            )
            return []

        if not self._has_required_fields():
            logger.error(
                f"Model {self.tracked_table.model_path} missing required timestamp fields"
            )
            return []

        last_polled = self.tracked_table.last_polled_at
        now = timezone.now()

        if last_polled is None:
            last_polled = now
            self.tracked_table.last_polled_at = now
            self.tracked_table.save(update_fields=["last_polled_at"])
            logger.info(
                f"First poll for {self.tracked_table.model_path} - "
                f"initialized last_polled_at to {now}, will track new changes only"
            )
            return []

        changes = []

        if self.tracked_table.event_type == TrackedTable.EventType.INSERT:
            changes = self._detect_inserts(last_polled)
        elif self.tracked_table.event_type == TrackedTable.EventType.UPDATE:
            changes = self._detect_updates(last_polled)

        self.tracked_table.last_polled_at = now
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

        Excludes newly created records (where created_at >= since).
        """
        queryset = self.model_class.objects.filter(
            updated_at__gt=since,
            created_at__lt=since,
        )

        changes = []
        for instance in queryset:
            changes.append(
                DetectedChange(
                    instance=instance,
                    event_type=TrackedTable.EventType.UPDATE,
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
    other tables.

    Returns:
        Number of notifications queued
    """
    active_tables = TrackedTable.objects.filter(
        is_active=True,
        channel__is_active=True,
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

    Each entry is written in its own transaction. A change is skipped when
    an entry for the same (tracked_table, source_pk, event_type) was created
    within the dedup window (DEDUP_WINDOW_SECONDS, 5 minutes by default).

    Args:
        tracked_table: TrackedTable configuration

    Returns:
        Number of notifications queued
    """
    poller = TablePoller(tracked_table)
    changes = poller.poll()

    queued = 0

    for change in changes:
        try:
            message = render_template(
                tracked_table.message_template,
                change.instance,
            )

            with transaction.atomic():
                if _is_duplicate(tracked_table, change):
                    logger.debug(
                        f"Skipping duplicate notification for "
                        f"{tracked_table.model_path}#{change.instance.pk}"
                    )
                    continue

                NotificationOutbox.objects.create(
                    tracked_table=tracked_table,
                    source_model=tracked_table.model_path,
                    source_pk=str(change.instance.pk),
                    event_type=change.event_type,
                    message=message,
                    status=NotificationOutbox.Status.PENDING,
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
            logger.exception(
                f"Error creating outbox entry for "
                f"{tracked_table.model_path}#{change.instance.pk}: {e}"
            )

    return queued


def _is_duplicate(tracked_table: TrackedTable, change: DetectedChange) -> bool:
    """
    Check if a notification for this change was already created recently.

    Prevents duplicate notifications if polling overlaps or retries occur.
    Checks for existing outbox entries within the dedup window
    (DEDUP_WINDOW_SECONDS, 300 seconds = 5 minutes by default).
    """
    window = timedelta(seconds=get_setting("DEDUP_WINDOW_SECONDS"))
    recent_cutoff = timezone.now() - window

    return NotificationOutbox.objects.filter(
        tracked_table=tracked_table,
        source_pk=str(change.instance.pk),
        event_type=change.event_type,
        created_at__gte=recent_cutoff,
    ).exists()
