"""
Explicit, transaction-safe event capture: ``webhook_outbox.enqueue()``.

The outbox entry is written with a plain INSERT in the caller's current
transaction. It commits or rolls back together with the business change, so
there is no window in which one exists without the other. Delivery runners
only see committed entries.
"""

import json
import logging
from datetime import datetime
from typing import Any

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models, transaction
from django.utils.module_loading import import_string

from webhook_outbox.conf import get_setting
from webhook_outbox.models import NotificationChannel, NotificationOutbox, TrackedTable
from webhook_outbox.services.channels.base import get_envelope_version
from webhook_outbox.services.template import render_template

logger = logging.getLogger(__name__)

EVENT_TYPE_MAX_LENGTH = NotificationOutbox._meta.get_field("event_type").max_length

# event type -> the row timestamp that marks that change (see the poller)
SOURCE_TIMESTAMP_FIELDS = {
    TrackedTable.EventType.INSERT: "created_at",
    TrackedTable.EventType.UPDATE: "updated_at",
}


class TrackedTableLookupError(LookupError):
    """enqueue() could not pick exactly one tracked table for the event."""


def enqueue(
    instance_or_ref,
    *,
    event_type: str,
    message: str | None = None,
    changes: dict[str, dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
    channel: NotificationChannel | int | None = None,
    tracked_table: TrackedTable | int | None = None,
) -> NotificationOutbox | None:
    """
    Queue one event in the current transaction.

    Args:
        instance_or_ref: a saved model instance, or ``(model, pk)`` where
            ``model`` is a model class or an ``"app_label.model"`` label
            (useful after a delete, or for rows changed with ``update()``).
        event_type: e.g. ``"insert"``, ``"update"``, ``"delete"`` or your own
            type (at most 10 characters).
        message: the message text. Default: the tracked table's
            ``message_template`` rendered with the instance (a ref is loaded
            from the database for that).
        changes: ``{"field": {"old": ..., "new": ...}}``. Envelope v2 only.
        extra: any JSON object of your own. Envelope v2 only.
        channel: pick the tracked table of this channel.
        tracked_table: use this tracked table (instance or pk). Required for
            event types other than insert/update/delete.

    Without ``tracked_table``, the tracked table is looked up by the source
    model, ``event_type`` and (if given) ``channel``. It must be exactly one
    active tracked table, else ``TrackedTableLookupError`` is raised; loop
    over tracked tables and pass ``tracked_table=`` to fan out.

    Returns:
        The new NotificationOutbox entry, or None when the tracked table or
        its channel is inactive (nothing is queued then).

    Raises:
        TrackedTableLookupError: no tracked table, or several active ones.
        ValueError: invalid arguments, e.g. ``changes`` for a v1 channel.
        TypeError: ``instance_or_ref`` is neither an instance nor a ref.

    The dedup window does not apply: every call queues an event. For
    ``"insert"`` / ``"update"`` the entry records the instance's
    ``created_at`` / ``updated_at``, so a poller of the same tracked table
    does not queue the same change again. Call ``enqueue()`` *after*
    ``save()`` for that.
    """
    model, pk, instance = _resolve_source(instance_or_ref)
    validate_event_type(event_type)
    changes = validate_changes(changes)
    extra = validate_extra(extra)

    tracked = _resolve_tracked_table(model, event_type, channel, tracked_table)
    if not (tracked.is_active and tracked.channel.is_active):
        logger.info(
            f"Not queueing {event_type!r} for {tracked.model_path}#{pk}: "
            f"tracked table {tracked.name!r} or its channel is inactive"
        )
        return None

    return create_entry(
        tracked,
        model=model,
        pk=pk,
        instance=instance,
        event_type=event_type,
        message=message,
        changes=changes,
        extra=extra,
    )


def create_entry(
    tracked: TrackedTable,
    *,
    model: type[models.Model],
    pk: Any,
    instance: models.Model | None,
    event_type: str,
    message: str | None = None,
    changes: dict | None = None,
    extra: dict | None = None,
    source_changed_at: datetime | None = None,
    use_source_timestamp: bool = True,
) -> NotificationOutbox:
    """Write one outbox entry for ``tracked`` (no lookup, no active check)."""
    if (changes is not None or extra is not None) and get_envelope_version(
        tracked.channel
    ) == 1:
        raise ValueError(
            f"Channel {tracked.channel.name!r} uses envelope v1, which cannot "
            "carry 'changes' or 'extra'. Set its config 'envelope_version' "
            "(or WEBHOOK_OUTBOX['ENVELOPE_VERSION']) to 2, or omit them."
        )

    if message is None:
        if instance is None:
            instance = model._base_manager.filter(pk=pk).first()
            if instance is None:
                raise ValueError(
                    f"{tracked.model_path}#{pk} does not exist: pass message= "
                    "to enqueue an event for a row that is gone"
                )
        message = render_template(tracked.message_template, instance)

    if source_changed_at is None and use_source_timestamp and instance is not None:
        source_changed_at = _source_timestamp(instance, event_type)

    entry = NotificationOutbox.objects.create(
        tracked_table=tracked,
        source_model=tracked.model_path,
        source_pk=str(pk),
        event_type=event_type,
        message=message,
        status=NotificationOutbox.Status.PENDING,
        source_changed_at=source_changed_at,
        changes=changes,
        extra=extra,
    )
    _schedule_hook(entry.pk)
    return entry


# --- validation -----------------------------------------------------------------


def validate_event_type(event_type: Any) -> None:
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("event_type must be a non-empty string")
    if len(event_type) > EVENT_TYPE_MAX_LENGTH:
        raise ValueError(
            f"event_type {event_type!r} is longer than "
            f"{EVENT_TYPE_MAX_LENGTH} characters"
        )


def _check_json(value: Any, name: str) -> None:
    try:
        json.dumps(value, cls=DjangoJSONEncoder)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name} is not JSON serialisable: {e}") from e


def validate_changes(changes: Any) -> dict | None:
    if changes is None:
        return None
    if not isinstance(changes, dict):
        raise ValueError('changes must be a dict: {"field": {"old": ..., "new": ...}}')
    for field, change in changes.items():
        if not isinstance(field, str):
            raise ValueError(f"changes: field name {field!r} is not a string")
        if not isinstance(change, dict) or set(change) != {"old", "new"}:
            raise ValueError(
                f"changes[{field!r}] must be a dict with exactly the keys "
                "'old' and 'new'"
            )
    _check_json(changes, "changes")
    return changes


def validate_extra(extra: Any) -> dict | None:
    if extra is None:
        return None
    if not isinstance(extra, dict):
        raise ValueError("extra must be a dict")
    _check_json(extra, "extra")
    return extra


# --- lookups --------------------------------------------------------------------


def _resolve_model(model_or_label) -> type[models.Model]:
    if isinstance(model_or_label, str):
        try:
            return apps.get_model(model_or_label)
        except (LookupError, ValueError) as e:
            raise ValueError(f"Unknown model {model_or_label!r}") from e
    if isinstance(model_or_label, type) and issubclass(model_or_label, models.Model):
        return model_or_label
    raise TypeError(f"{model_or_label!r} is not a model class or 'app_label.model'")


def _resolve_source(instance_or_ref):
    if isinstance(instance_or_ref, models.Model):
        if instance_or_ref.pk is None:
            raise ValueError("enqueue() needs a saved instance (pk is None)")
        return type(instance_or_ref), instance_or_ref.pk, instance_or_ref
    if isinstance(instance_or_ref, (tuple, list)) and len(instance_or_ref) == 2:
        model_or_label, pk = instance_or_ref
        if pk is None:
            raise ValueError("enqueue() needs a primary key (got None)")
        return _resolve_model(model_or_label), pk, None
    raise TypeError(
        "enqueue() expects a model instance or a (model, pk) tuple, "
        f"got {instance_or_ref!r}"
    )


def _resolve_tracked_table(model, event_type, channel, tracked_table) -> TrackedTable:
    content_type = ContentType.objects.get_for_model(model)

    if tracked_table is not None:
        if not isinstance(tracked_table, TrackedTable):
            tracked_table = TrackedTable.objects.select_related(
                "channel", "content_type"
            ).get(pk=tracked_table)
        if tracked_table.content_type_id != content_type.pk:
            raise ValueError(
                f"Tracked table {tracked_table.name!r} tracks "
                f"{tracked_table.model_path}, not {content_type.app_label}."
                f"{content_type.model}"
            )
        channel_pk = getattr(channel, "pk", channel)
        if channel_pk is not None and tracked_table.channel_id != channel_pk:
            raise ValueError(
                f"Tracked table {tracked_table.name!r} does not belong to "
                f"channel {channel!r}"
            )
        return tracked_table

    candidates = TrackedTable.objects.filter(
        content_type=content_type, event_type=event_type
    ).select_related("channel", "content_type")
    if channel is not None:
        candidates = candidates.filter(channel=channel)
    candidates = list(candidates.order_by("pk"))

    label = f"{content_type.app_label}.{content_type.model}"
    if not candidates:
        raise TrackedTableLookupError(
            f"No tracked table for {label} with event type {event_type!r}"
            + (f" and channel {channel!r}" if channel is not None else "")
            + "; create one or pass tracked_table="
        )
    active = [t for t in candidates if t.is_active and t.channel.is_active]
    if len(active) > 1:
        raise TrackedTableLookupError(
            f"{len(active)} active tracked tables for {label} with event type "
            f"{event_type!r}; pass channel= or tracked_table="
        )
    return active[0] if active else candidates[0]


def _source_timestamp(instance: models.Model, event_type: str) -> datetime | None:
    field_name = SOURCE_TIMESTAMP_FIELDS.get(event_type)
    if field_name is None:
        return None
    value = getattr(instance, field_name, None)
    return value if isinstance(value, datetime) else None


# --- on_commit hook -------------------------------------------------------------


def _get_hook():
    hook = get_setting("ENQUEUE_HOOK")
    if hook is None:
        return None
    return import_string(hook) if isinstance(hook, str) else hook


def _schedule_hook(outbox_id: int) -> None:
    try:
        hook = _get_hook()
    except Exception:
        logger.exception("ENQUEUE_HOOK cannot be loaded")
        return
    if hook is None:
        return

    def run():
        try:
            hook(outbox_id)
        except Exception:
            # The entry is committed and pending: a delivery pass sends it.
            logger.exception(f"ENQUEUE_HOOK failed for entry #{outbox_id}")

    transaction.on_commit(run)
