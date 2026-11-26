"""
Settings of the app.

Everything lives in one dict setting::

    WEBHOOK_OUTBOX = {
        "MAX_RETRIES": 3,
        "WEBHOOK_TIMEOUT": 30,
        ...
    }

A few keys fall back to the legacy flat Django settings of the code this app
was extracted from, so a project can switch to the package without renaming
its settings first:

    MAX_RETRIES      <- NOTIFICATION_MAX_RETRIES
    WEBHOOK_TIMEOUT  <- NOTIFICATION_WEBHOOK_TIMEOUT
    SITE_URL         <- SITE_URL

Values are read on every access (no caching), so ``override_settings`` works
in tests.
"""

from copy import deepcopy
from typing import Any

from django.conf import settings

SETTING_NAME = "WEBHOOK_OUTBOX"

DEFAULT_CHANNELS: dict[str, str] = {
    "webhook": "webhook_outbox.services.channels.webhook.WebhookChannel",
}

DEFAULTS: dict[str, Any] = {
    # Delivery attempts before an outbox entry is marked FAILED.
    "MAX_RETRIES": 3,
    # Default HTTP timeout (seconds) of a webhook channel without "timeout".
    "WEBHOOK_TIMEOUT": 30,
    # Base URL prepended to {admin_link} in message templates.
    "SITE_URL": "",
    # An event for the same (tracked table, row, event type) is not queued
    # again if one was queued within this many seconds.
    "DEDUP_WINDOW_SECONDS": 300,
    # Outbox entries handled by one delivery pass.
    "DELIVERY_BATCH_SIZE": 100,
    # Intervals (seconds) of the worker loop / suggested beat schedule.
    "POLL_INTERVAL": 30,
    "SEND_INTERVAL": 10,
    # channel_type -> BaseChannel subclass (dotted path or class). Merged
    # over DEFAULT_CHANNELS; map a type to None to remove it.
    "CHANNELS": {},
    # App labels (or full app names) never offered as trackable in the admin,
    # on top of django.* and webhook_outbox itself.
    "TRACKABLE_EXCLUDE_APPS": [],
    # Dotted path to a callable(outbox_id) called by the admin "retry" action
    # after an entry was reset to pending, e.g. to push it right away.
    "ADMIN_RETRY_HOOK": None,
    # --- 1.1 ---------------------------------------------------------------
    # The poll cursor trails "now" by this many seconds, so a row committed up
    # to this long after its created_at/updated_at is still seen (the overlap
    # is de-duplicated exactly). 0 = the 1.0 cursor (late commits are missed).
    "POLL_OVERLAP_SECONDS": 60,
    # Envelope POSTed by the webhook channel: 1 (the 1.0 document) or 2. A
    # channel can override it with "envelope_version" in its config.
    "ENVELOPE_VERSION": 1,
    # Add "event_id" as a seventh key to envelope v1 (v2 always has it).
    "ENVELOPE_V1_EVENT_ID": False,
    # Models whose post_save/post_delete signals queue events, e.g.
    # ["shop.order"] or {"shop.order": {"fields": ["status"]}} ("fields":
    # whose old/new values go to "changes" on update). Empty = off.
    "SIGNAL_CAPTURE": {},
    # Dotted path to a callable(outbox_id) called on commit of the
    # transaction in which enqueue() (or signal capture) queued an entry.
    "ENQUEUE_HOOK": None,
    # --- 1.2 ---------------------------------------------------------------
    # Field names (fnmatch patterns, case-insensitive) that tracked table
    # filters may not use, on any model along the path.
    "PREDICATE_DENY_FIELDS": ["*password*", "*token*", "*secret*", "*api_key*"],
}

LEGACY_SETTINGS: dict[str, str] = {
    "MAX_RETRIES": "NOTIFICATION_MAX_RETRIES",
    "WEBHOOK_TIMEOUT": "NOTIFICATION_WEBHOOK_TIMEOUT",
    "SITE_URL": "SITE_URL",
}


def get_user_settings() -> dict[str, Any]:
    value = getattr(settings, SETTING_NAME, None)
    return value if isinstance(value, dict) else {}


def get_setting(name: str) -> Any:
    """
    Resolve one setting: ``WEBHOOK_OUTBOX[name]``, then the legacy Django
    setting (if any), then the default.
    """
    if name not in DEFAULTS:
        raise KeyError(f"Unknown {SETTING_NAME} setting: {name}")

    user_settings = get_user_settings()
    if name in user_settings:
        return user_settings[name]

    legacy = LEGACY_SETTINGS.get(name)
    if legacy is not None and hasattr(settings, legacy):
        return getattr(settings, legacy)

    return deepcopy(DEFAULTS[name])


def get_channel_paths() -> dict[str, Any]:
    """channel_type -> dotted path or class, defaults merged with CHANNELS."""
    channels: dict[str, Any] = dict(DEFAULT_CHANNELS)
    configured = get_setting("CHANNELS") or {}
    if isinstance(configured, dict):
        channels.update(configured)
    return {key: value for key, value in channels.items() if value is not None}


class AppSettings:
    """Attribute access to the settings: ``app_settings.MAX_RETRIES``."""

    def __getattr__(self, name: str) -> Any:
        if name not in DEFAULTS:
            raise AttributeError(name)
        return get_setting(name)


app_settings = AppSettings()
