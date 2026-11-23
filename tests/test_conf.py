import pytest
from django.test import override_settings

from webhook_outbox.conf import DEFAULTS, app_settings, get_channel_paths, get_setting


def test_defaults():
    assert DEFAULTS == {
        "MAX_RETRIES": 3,
        "WEBHOOK_TIMEOUT": 30,
        "SITE_URL": "",
        "DEDUP_WINDOW_SECONDS": 300,
        "DELIVERY_BATCH_SIZE": 100,
        "POLL_INTERVAL": 30,
        "SEND_INTERVAL": 10,
        "CHANNELS": {},
        "TRACKABLE_EXCLUDE_APPS": [],
        "ADMIN_RETRY_HOOK": None,
        "POLL_OVERLAP_SECONDS": 60,
        "ENVELOPE_VERSION": 1,
        "ENVELOPE_V1_EVENT_ID": False,
        "SIGNAL_CAPTURE": {},
        "ENQUEUE_HOOK": None,
    }
    for key, value in DEFAULTS.items():
        assert get_setting(key) == value


@pytest.mark.parametrize(
    ("key", "legacy"),
    [
        ("MAX_RETRIES", "NOTIFICATION_MAX_RETRIES"),
        ("WEBHOOK_TIMEOUT", "NOTIFICATION_WEBHOOK_TIMEOUT"),
        ("SITE_URL", "SITE_URL"),
    ],
)
def test_legacy_fallback_and_precedence(key, legacy):
    with override_settings(**{legacy: "legacy"}):
        assert get_setting(key) == "legacy"
        with override_settings(WEBHOOK_OUTBOX={key: "new"}):
            assert get_setting(key) == "new"


def test_no_legacy_for_new_keys():
    with override_settings(
        NOTIFICATION_POLL_INTERVAL=99, NOTIFICATION_SEND_INTERVAL=99
    ):
        assert get_setting("POLL_INTERVAL") == 30
        assert get_setting("SEND_INTERVAL") == 10


def test_attribute_access():
    with override_settings(WEBHOOK_OUTBOX={"DELIVERY_BATCH_SIZE": 5}):
        assert app_settings.DELIVERY_BATCH_SIZE == 5
    with pytest.raises(AttributeError):
        app_settings.NOPE  # noqa: B018


def test_unknown_key_raises():
    with pytest.raises(KeyError):
        get_setting("NOPE")


def test_defaults_are_not_shared_mutables():
    get_setting("TRACKABLE_EXCLUDE_APPS").append("x")
    assert get_setting("TRACKABLE_EXCLUDE_APPS") == []


def test_channel_paths_merge():
    assert get_channel_paths() == {
        "webhook": "webhook_outbox.services.channels.webhook.WebhookChannel"
    }
    with override_settings(WEBHOOK_OUTBOX={"CHANNELS": {"slack": "x.Slack"}}):
        assert set(get_channel_paths()) == {"webhook", "slack"}
    with override_settings(WEBHOOK_OUTBOX={"CHANNELS": {"webhook": None}}):
        assert get_channel_paths() == {}
