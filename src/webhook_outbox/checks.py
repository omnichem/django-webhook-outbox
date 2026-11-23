"""
System checks.

Configuration checks (always run):

* webhook_outbox.E001 - WEBHOOK_OUTBOX is not a dict
* webhook_outbox.W001 - unknown key in WEBHOOK_OUTBOX
* webhook_outbox.E002 - CHANNELS is not a dict
* webhook_outbox.E003 - a CHANNELS entry is not a loadable BaseChannel subclass
* webhook_outbox.E004 - TRACKABLE_EXCLUDE_APPS is not a list of strings
* webhook_outbox.E005 - ADMIN_RETRY_HOOK is not an importable callable
* webhook_outbox.E006 - a numeric setting has the wrong type or is out of range

Database checks (``manage.py check --database default``, and ``migrate``).
They only warn: a stale row must not block deployments.

* webhook_outbox.W010 - a channel uses a channel type with no registered class
* webhook_outbox.W011 - a tracked table points to a model that no longer exists
* webhook_outbox.W012 - a tracked model lacks the timestamp fields its event
  type needs (INSERT: created_at; UPDATE: created_at and updated_at)
* webhook_outbox.W013 - a tracked table tracks webhook_outbox's own models
  (self-tracking feedback loop)
"""

from django.core.checks import Error, Tags, Warning, register
from django.db import DatabaseError, connections
from django.utils.module_loading import import_string

from .conf import DEFAULTS, SETTING_NAME, get_channel_paths, get_setting

SELF_APP_LABEL = "webhook_outbox"

# setting -> (type, smallest allowed value, description)
NUMERIC_SETTINGS = {
    "MAX_RETRIES": (int, 1, "an integer >= 1"),
    "DELIVERY_BATCH_SIZE": (int, 1, "an integer >= 1"),
    "DEDUP_WINDOW_SECONDS": (int, 0, "an integer >= 0"),
    "WEBHOOK_TIMEOUT": ((int, float), 0, "a number > 0"),
    "POLL_INTERVAL": ((int, float), 0, "a number > 0"),
    "SEND_INTERVAL": ((int, float), 0, "a number > 0"),
}


@register()
def check_settings(app_configs=None, **kwargs):
    from django.conf import settings

    errors = []
    raw = getattr(settings, SETTING_NAME, None)
    if raw is not None and not isinstance(raw, dict):
        return [
            Error(
                f"{SETTING_NAME} must be a dict.",
                id="webhook_outbox.E001",
            )
        ]
    for key in sorted(raw or {}):
        if key not in DEFAULTS:
            errors.append(
                Warning(
                    f"Unknown {SETTING_NAME} key: {key!r}.",
                    hint=f"Known keys: {', '.join(sorted(DEFAULTS))}.",
                    id="webhook_outbox.W001",
                )
            )

    channels = get_setting("CHANNELS")
    if channels is not None and not isinstance(channels, dict):
        errors.append(
            Error(
                f"{SETTING_NAME}['CHANNELS'] must be a dict of "
                "channel_type -> BaseChannel subclass (or dotted path).",
                id="webhook_outbox.E002",
            )
        )
    else:
        from .services.channels import load_channel_class

        for channel_type, path in get_channel_paths().items():
            try:
                load_channel_class(path)
            except Exception as e:
                errors.append(
                    Error(
                        f"Channel type {channel_type!r}: cannot load {path!r} ({e}).",
                        hint="Point it to a subclass of "
                        "webhook_outbox.services.channels.BaseChannel.",
                        id="webhook_outbox.E003",
                    )
                )

    exclude = get_setting("TRACKABLE_EXCLUDE_APPS")
    if not isinstance(exclude, (list, tuple)) or not all(
        isinstance(item, str) for item in exclude
    ):
        errors.append(
            Error(
                f"{SETTING_NAME}['TRACKABLE_EXCLUDE_APPS'] must be a list of "
                "app labels or app names.",
                id="webhook_outbox.E004",
            )
        )

    hook = get_setting("ADMIN_RETRY_HOOK")
    if hook is not None:
        try:
            func = import_string(hook) if isinstance(hook, str) else hook
            if not callable(func):
                raise TypeError("not callable")
        except Exception as e:
            errors.append(
                Error(
                    f"{SETTING_NAME}['ADMIN_RETRY_HOOK'] {hook!r} is not an "
                    f"importable callable ({e}).",
                    id="webhook_outbox.E005",
                )
            )

    for name, (types, minimum, description) in NUMERIC_SETTINGS.items():
        value = get_setting(name)
        valid = (
            isinstance(value, types)
            and not isinstance(value, bool)
            and (value >= minimum if types is int else value > minimum)
        )
        if not valid:
            errors.append(
                Error(
                    f"{SETTING_NAME}[{name!r}] must be {description}, "
                    f"got {value!r}.",
                    id="webhook_outbox.E006",
                )
            )

    return errors


def _tables_exist(alias: str) -> bool:
    from .models import NotificationChannel, TrackedTable

    try:
        with connections[alias].cursor() as cursor:
            existing = set(connections[alias].introspection.table_names(cursor))
    except DatabaseError:
        return False
    return {NotificationChannel._meta.db_table, TrackedTable._meta.db_table} <= existing


@register(Tags.database)
def check_database(app_configs=None, databases=None, **kwargs):
    from .models import NotificationChannel, TrackedTable
    from .services.channels import get_channel_registry

    errors = []
    registry = get_channel_registry()

    for alias in databases or ():
        if not _tables_exist(alias):
            continue

        unknown = (
            NotificationChannel.objects.using(alias)
            .exclude(channel_type__in=list(registry))
            .order_by("pk")
        )
        for channel in unknown:
            errors.append(
                Warning(
                    f"Notification channel #{channel.pk} ({channel.name!r}) has "
                    f"channel type {channel.channel_type!r} with no registered "
                    "channel class; its notifications will fail.",
                    hint=f"Register it in {SETTING_NAME}['CHANNELS'].",
                    obj=channel,
                    id="webhook_outbox.W010",
                )
            )

        tracked_tables = (
            TrackedTable.objects.using(alias)
            .select_related("content_type")
            .order_by("pk")
        )
        for tracked in tracked_tables:
            errors.extend(_check_tracked_table(tracked))

    return errors


def _check_tracked_table(tracked) -> list:
    from .models import TrackedTable

    if tracked.content_type.app_label == SELF_APP_LABEL:
        return [
            Warning(
                f"Tracked table #{tracked.pk} ({tracked.name!r}) tracks "
                f"{tracked.model_path}, a model of webhook_outbox itself: "
                "every notification would create another one.",
                hint="Delete or deactivate this tracked table.",
                obj=tracked,
                id="webhook_outbox.W013",
            )
        ]

    model_class = tracked.content_type.model_class()
    if model_class is None:
        return [
            Warning(
                f"Tracked table #{tracked.pk} ({tracked.name!r}) points to "
                f"{tracked.model_path}, which is not an installed model; it "
                "is skipped by the poller.",
                obj=tracked,
                id="webhook_outbox.W011",
            )
        ]

    field_names = {f.name for f in model_class._meta.get_fields()}
    required = ["created_at"]
    if tracked.event_type == TrackedTable.EventType.UPDATE:
        required.append("updated_at")
    missing = [name for name in required if name not in field_names]
    if missing:
        return [
            Warning(
                f"Tracked table #{tracked.pk} ({tracked.name!r}) tracks "
                f"{tracked.event_type.upper()} on {tracked.model_path}, which "
                f"has no {' / '.join(missing)} field; it is skipped by the poller.",
                hint="Add created_at/updated_at (e.g. inherit "
                "webhook_outbox.models.TimeStampedModel).",
                obj=tracked,
                id="webhook_outbox.W012",
            )
        ]
    return []
