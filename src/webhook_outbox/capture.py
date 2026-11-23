"""
Optional signal capture (off by default).

``WEBHOOK_OUTBOX["SIGNAL_CAPTURE"]`` lists models whose ``post_save`` and
``post_delete`` signals queue events, in the transaction of the save or
delete::

    WEBHOOK_OUTBOX = {
        "SIGNAL_CAPTURE": {
            "shop.order": {"fields": ["status", "total"]},
            "shop.customer": {},
        },
    }

A list of labels (``["shop.order"]``) is the same as a dict with empty
options. ``"fields"`` names the concrete fields whose old and new values go
into ``changes`` on update (envelope v2 channels only). The old values are
read from the database in ``pre_save``, so this costs one query per save.
Without ``"fields"``, ``changes`` is null.

A create queues an ``insert`` event for every active INSERT tracked table of
the model, an update an ``update`` event for every active UPDATE tracked
table, a delete a ``delete`` event for every active DELETE tracked table.

Capture is best-effort: it does not see ``QuerySet.update()``,
``bulk_create()``, ``bulk_update()`` or raw SQL, and a failure to queue is
logged instead of failing the save (see docs/semantics.md).
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models, transaction
from django.db.models.signals import post_delete, post_save, pre_save

from webhook_outbox.conf import SETTING_NAME, get_setting

logger = logging.getLogger(__name__)

SELF_APP_LABEL = "webhook_outbox"
OLD_VALUES_ATTR = "_webhook_outbox_old_values"


@dataclass(frozen=True)
class CaptureOptions:
    fields: tuple[models.Field, ...] = field(default_factory=tuple)


def get_capture_config() -> tuple[dict[type[models.Model], CaptureOptions], list[str]]:
    """
    Parse SIGNAL_CAPTURE. Returns ({model: options}, [error messages]);
    invalid entries are left out of the mapping.
    """
    raw = get_setting("SIGNAL_CAPTURE")
    if not raw:
        return {}, []
    if isinstance(raw, (list, tuple)):
        raw = {label: {} for label in raw}
    if not isinstance(raw, dict):
        return {}, [
            f"{SETTING_NAME}['SIGNAL_CAPTURE'] must be a dict of "
            "'app_label.model' -> options, or a list of 'app_label.model'."
        ]

    config: dict[type[models.Model], CaptureOptions] = {}
    errors: list[str] = []
    for label, options in raw.items():
        prefix = f"{SETTING_NAME}['SIGNAL_CAPTURE'][{label!r}]"
        try:
            model = apps.get_model(label)
        except (LookupError, ValueError, TypeError):
            errors.append(f"{prefix}: unknown model.")
            continue
        if model._meta.app_label == SELF_APP_LABEL:
            errors.append(
                f"{prefix}: webhook_outbox's own models cannot be captured "
                "(every notification would create another one)."
            )
            continue
        if options is None:
            options = {}
        if not isinstance(options, dict) or set(options) - {"fields"}:
            errors.append(f'{prefix}: options must be a dict like {{"fields": [...]}}.')
            continue
        names = options.get("fields") or []
        if not isinstance(names, (list, tuple)) or not all(
            isinstance(name, str) for name in names
        ):
            errors.append(f"{prefix}['fields'] must be a list of field names.")
            continue
        fields = []
        for name in names:
            try:
                model_field = model._meta.get_field(name)
            except Exception:
                model_field = None
            if model_field is None or not getattr(model_field, "concrete", False):
                errors.append(f"{prefix}['fields']: {name!r} is not a concrete field.")
                continue
            if model_field.many_to_many:
                errors.append(
                    f"{prefix}['fields']: {name!r} is many-to-many (not supported)."
                )
                continue
            fields.append(model_field)
        config[model] = CaptureOptions(fields=tuple(fields))
    return config, errors


# --- connection -----------------------------------------------------------------

_connected: dict[type[models.Model], CaptureOptions] = {}


def _uid(signal_name: str, model: type[models.Model]) -> str:
    return f"webhook_outbox.capture.{signal_name}.{model._meta.label_lower}"


def disconnect() -> None:
    for model in list(_connected):
        pre_save.disconnect(sender=model, dispatch_uid=_uid("pre_save", model))
        post_save.disconnect(sender=model, dispatch_uid=_uid("post_save", model))
        post_delete.disconnect(sender=model, dispatch_uid=_uid("post_delete", model))
    _connected.clear()


def connect() -> None:
    """(Re)connect the signal handlers from the current SIGNAL_CAPTURE."""
    disconnect()
    config, errors = get_capture_config()
    for error in errors:
        logger.error(f"Signal capture: {error} (skipped)")
    for model, options in config.items():
        if options.fields:
            pre_save.connect(
                _pre_save, sender=model, dispatch_uid=_uid("pre_save", model)
            )
        post_save.connect(
            _post_save, sender=model, dispatch_uid=_uid("post_save", model)
        )
        post_delete.connect(
            _post_delete, sender=model, dispatch_uid=_uid("post_delete", model)
        )
        _connected[model] = options


def on_setting_changed(setting, **kwargs) -> None:
    """Keeps override_settings(WEBHOOK_OUTBOX=...) working in tests."""
    if setting == SETTING_NAME:
        connect()


# --- handlers -------------------------------------------------------------------


def _pre_save(sender, instance, raw=False, using=None, **kwargs):
    options = _connected.get(sender)
    if raw or options is None or not options.fields:
        return
    if instance._state.adding or instance.pk is None:
        return
    attnames = [f.attname for f in options.fields]
    try:
        old = (
            sender._base_manager.using(using)
            .filter(pk=instance.pk)
            .values(*attnames)
            .first()
        )
    except Exception:
        logger.exception(f"Signal capture: cannot read old values of {instance!r}")
        return
    instance.__dict__[OLD_VALUES_ATTR] = old


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, cls=DjangoJSONEncoder)
    except (TypeError, ValueError):
        return str(value)
    return value


def _changes(options: CaptureOptions, instance, old: dict | None) -> dict | None:
    if not options.fields or old is None:
        return None
    changes = {}
    for model_field in options.fields:
        before = old.get(model_field.attname)
        after = getattr(instance, model_field.attname)
        if before != after:
            changes[model_field.name] = {
                "old": _jsonable(before),
                "new": _jsonable(after),
            }
    return changes


def _post_save(sender, instance, created, raw=False, update_fields=None, **kwargs):
    old = instance.__dict__.pop(OLD_VALUES_ATTR, None)
    options = _connected.get(sender)
    if raw or options is None:
        return
    from webhook_outbox.models import TrackedTable

    if created:
        _capture(sender, instance, TrackedTable.EventType.INSERT.value, None)
    else:
        try:
            changes = _changes(options, instance, old)
        except Exception:
            logger.exception(f"Signal capture: cannot compare values of {instance!r}")
            changes = None
        # save(update_fields=[...]) without updated_at leaves the column as
        # it was: do not record a stale timestamp for this event.
        use_timestamp = update_fields is None or "updated_at" in update_fields
        _capture(
            sender,
            instance,
            TrackedTable.EventType.UPDATE.value,
            changes,
            use_timestamp=use_timestamp,
        )


def _post_delete(sender, instance, **kwargs):
    if sender not in _connected:
        return
    from webhook_outbox.models import TrackedTable

    _capture(sender, instance, TrackedTable.EventType.DELETE.value, None)


def _capture(model, instance, event_type, changes, *, use_timestamp=True) -> None:
    from webhook_outbox.models import TrackedTable
    from webhook_outbox.services.channels.base import get_envelope_version
    from webhook_outbox.services.enqueue import create_entry

    try:
        # A savepoint: a failure here must not break the caller's transaction.
        with transaction.atomic():
            tracked_tables = list(
                TrackedTable.objects.filter(
                    content_type=ContentType.objects.get_for_model(model),
                    event_type=event_type,
                    is_active=True,
                    channel__is_active=True,
                ).select_related("channel", "content_type")
            )
    except Exception:
        logger.exception(f"Signal capture: cannot look up tracked tables of {model}")
        return

    for tracked in tracked_tables:
        try:
            with transaction.atomic():
                create_entry(
                    tracked,
                    model=model,
                    pk=instance.pk,
                    instance=instance,
                    event_type=event_type,
                    changes=(
                        changes if get_envelope_version(tracked.channel) == 2 else None
                    ),
                    use_source_timestamp=use_timestamp,
                )
        except Exception:
            logger.exception(
                f"Signal capture: cannot queue {event_type!r} of "
                f"{tracked.model_path}#{instance.pk} for {tracked.name!r}"
            )
