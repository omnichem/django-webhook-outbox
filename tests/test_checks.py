import pytest
from django.contrib.contenttypes.models import ContentType
from django.core import checks
from django.test import override_settings

from tests.factories import create_channel, create_tracked_table
from tests.testapp.models import Company, InsertOnlyItem, PlainItem
from webhook_outbox.models import NotificationOutbox


def run_checks(databases=None):
    return checks.run_checks(databases=databases)


def ids(messages):
    return sorted(m.id for m in messages if m.id and m.id.startswith("webhook_outbox."))


def test_clean_project_has_no_messages():
    assert ids(run_checks()) == []


@override_settings(WEBHOOK_OUTBOX=["not", "a", "dict"])
def test_setting_must_be_dict():
    assert ids(run_checks()) == ["webhook_outbox.E001"]


@override_settings(WEBHOOK_OUTBOX={"MAX_RETRY": 5})
def test_unknown_key():
    assert ids(run_checks()) == ["webhook_outbox.W001"]


@override_settings(WEBHOOK_OUTBOX={"CHANNELS": ["webhook"]})
def test_channels_must_be_dict():
    assert ids(run_checks()) == ["webhook_outbox.E002"]


@override_settings(
    WEBHOOK_OUTBOX={
        "CHANNELS": {
            "missing": "tests.channels.DoesNotExist",
            "wrong": "tests.channels.NotAChannel",
            "ok": "tests.channels.RecordingChannel",
        }
    }
)
def test_unloadable_channel_classes():
    assert ids(run_checks()) == ["webhook_outbox.E003", "webhook_outbox.E003"]


@override_settings(WEBHOOK_OUTBOX={"TRACKABLE_EXCLUDE_APPS": "testapp"})
def test_exclude_apps_must_be_list():
    assert ids(run_checks()) == ["webhook_outbox.E004"]


@pytest.mark.parametrize("hook", ["tests.channels.nope", "tests.channels.SENT"])
def test_admin_retry_hook_must_be_callable(hook):
    with override_settings(WEBHOOK_OUTBOX={"ADMIN_RETRY_HOOK": hook}):
        assert ids(run_checks()) == ["webhook_outbox.E005"]


@override_settings(WEBHOOK_OUTBOX={"ADMIN_RETRY_HOOK": "tests.channels.retry_hook"})
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DEDUP_WINDOW_SECONDS", "300"),
        ("DEDUP_WINDOW_SECONDS", -1),
        ("DEDUP_WINDOW_SECONDS", 1.5),
        ("MAX_RETRIES", "3"),
        ("MAX_RETRIES", 0),
        ("MAX_RETRIES", True),
        ("DELIVERY_BATCH_SIZE", -5),
        ("WEBHOOK_TIMEOUT", "30"),
        ("WEBHOOK_TIMEOUT", 0),
        ("POLL_INTERVAL", None),
        ("SEND_INTERVAL", -1),
    ],
)
def test_numeric_settings_are_validated(key, value):
    with override_settings(WEBHOOK_OUTBOX={key: value}):
        assert ids(run_checks()) == ["webhook_outbox.E006"]


@pytest.mark.parametrize(
    "name", ["NOTIFICATION_MAX_RETRIES", "NOTIFICATION_WEBHOOK_TIMEOUT"]
)
def test_numeric_legacy_settings_are_validated(name):
    with override_settings(**{name: "3"}):
        assert ids(run_checks()) == ["webhook_outbox.E006"]


@override_settings(
    WEBHOOK_OUTBOX={
        "MAX_RETRIES": 5,
        "DEDUP_WINDOW_SECONDS": 0,
        "WEBHOOK_TIMEOUT": 2.5,
        "POLL_INTERVAL": 0.5,
    }
)
def test_valid_numeric_settings():
    assert ids(run_checks()) == []


def test_admin_retry_hook_ok():
    assert ids(run_checks()) == []


# --- database checks ------------------------------------------------------------


@pytest.mark.django_db
def test_database_checks_need_database_flag():
    create_channel(channel_type="carrier-pigeon")
    assert ids(run_checks()) == []
    assert ids(run_checks(databases=["default"])) == ["webhook_outbox.W010"]


@pytest.mark.django_db
@override_settings(
    WEBHOOK_OUTBOX={"CHANNELS": {"pigeon": "tests.channels.RecordingChannel"}}
)
def test_registered_custom_channel_type_is_known():
    create_channel(channel_type="pigeon")
    assert ids(run_checks(databases=["default"])) == []


@pytest.mark.django_db
def test_missing_timestamp_fields():
    channel = create_channel()
    create_tracked_table(PlainItem, "insert", channel=channel)
    create_tracked_table(InsertOnlyItem, "update", channel=channel)
    create_tracked_table(InsertOnlyItem, "insert", channel=channel)  # fine
    create_tracked_table(Company, "update", channel=channel)  # fine

    assert ids(run_checks(databases=["default"])) == [
        "webhook_outbox.W012",
        "webhook_outbox.W012",
    ]


@pytest.mark.django_db
def test_stale_content_type():
    ct = ContentType.objects.create(app_label="gone", model="model")
    create_tracked_table(Company, content_type=ct)
    assert ids(run_checks(databases=["default"])) == ["webhook_outbox.W011"]


@pytest.mark.django_db
def test_self_tracking():
    create_tracked_table(NotificationOutbox)
    messages = run_checks(databases=["default"])
    assert ids(messages) == ["webhook_outbox.W013"]
    assert "webhook_outbox.notificationoutbox" in messages[0].msg
