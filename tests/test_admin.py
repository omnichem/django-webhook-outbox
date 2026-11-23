import pytest
from django.contrib.admin.sites import site
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from tests import channels as test_channels
from tests.factories import create_channel, create_outbox_entry, create_tracked_table
from tests.testapp.models import Company, InsertOnlyItem, PlainItem, RegistrationRequest
from webhook_outbox.admin import (
    NotificationChannelForm,
    TrackedTableForm,
    get_trackable_content_types,
)
from webhook_outbox.models import NotificationChannel, NotificationOutbox, TrackedTable

pytestmark = pytest.mark.django_db

Status = NotificationOutbox.Status


@pytest.fixture
def admin_client(client):
    user = User.objects.create_superuser("admin", "admin@example.com", "pw")
    client.force_login(user)
    return client


@pytest.fixture(autouse=True)
def _reset_recorder():
    test_channels.SENT.clear()
    yield
    test_channels.SENT.clear()


def ct(model):
    return ContentType.objects.get_for_model(model)


# --- trackable content types ----------------------------------------------------


def test_trackable_choices():
    choices = dict(get_trackable_content_types())

    assert choices[ct(Company).pk] == "companies"
    assert choices[ct(InsertOnlyItem).pk] == "insert only items (INSERT only)"
    assert ct(PlainItem).pk not in choices
    for model in (NotificationChannel, TrackedTable, NotificationOutbox, User):
        assert ct(model).pk not in choices
    labels = [label for _pk, label in get_trackable_content_types()]
    assert labels == sorted(labels)


@pytest.mark.parametrize("excluded", ["testapp", "tests.testapp"])
def test_trackable_exclude_apps(excluded):
    with override_settings(WEBHOOK_OUTBOX={"TRACKABLE_EXCLUDE_APPS": [excluded]}):
        assert get_trackable_content_types() == []


def _tracked_form_data(content_type, channel, **overrides):
    data = {
        "name": "Rule",
        "content_type": content_type.pk,
        "event_type": "insert",
        "message_template": "New {company_name}",
        "channel": channel.pk,
        "is_active": "on",
    }
    data.update(overrides)
    return data


def test_tracked_table_form_accepts_trackable_model():
    form = TrackedTableForm(data=_tracked_form_data(ct(Company), create_channel()))
    assert form.is_valid(), form.errors


@pytest.mark.parametrize("model", [NotificationOutbox, PlainItem, User])
def test_tracked_table_form_rejects_untrackable_models(model):
    """Self-tracking guard: a posted id outside the choices is rejected."""
    form = TrackedTableForm(data=_tracked_form_data(ct(model), create_channel()))
    assert not form.is_valid()
    assert "content_type" in form.errors


# --- channel types --------------------------------------------------------------


def test_channel_form_choices_default():
    form = NotificationChannelForm()
    assert form.fields["channel_type"].choices == [("webhook", "Webhook")]


@override_settings(
    WEBHOOK_OUTBOX={"CHANNELS": {"recording": "tests.channels.RecordingChannel"}}
)
def test_channel_form_custom_type():
    form = NotificationChannelForm(
        data={
            "name": "Rec",
            "channel_type": "recording",
            "config": '{"room": "ops"}',
            "is_active": "on",
        }
    )
    assert form.fields["channel_type"].choices == [
        ("webhook", "Webhook"),
        ("recording", "Recording"),
    ]
    assert form.is_valid(), form.errors
    channel = form.save()
    assert channel.channel_type == "recording"


def test_channel_form_rejects_unregistered_type():
    form = NotificationChannelForm(
        data={
            "name": "X",
            "channel_type": "pigeon",
            "config": '{"a": 1}',
            "is_active": "on",
        }
    )
    assert not form.is_valid()
    assert "channel_type" in form.errors


# --- pages ----------------------------------------------------------------------


def test_admin_pages_render(admin_client):
    channel = create_channel(name="Hooks")
    tracked = create_tracked_table(Company, channel=channel)
    entry = create_outbox_entry(tracked)

    for model in (NotificationChannel, TrackedTable, NotificationOutbox):
        opts = model._meta
        url = reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist")
        assert admin_client.get(url).status_code == 200

    assert (
        admin_client.get(
            reverse(
                "admin:webhook_outbox_notificationchannel_change", args=[channel.pk]
            )
        ).status_code
        == 200
    )
    assert (
        admin_client.get(
            reverse("admin:webhook_outbox_trackedtable_change", args=[tracked.pk])
        ).status_code
        == 200
    )
    assert (
        admin_client.get(reverse("admin:webhook_outbox_trackedtable_add")).status_code
        == 200
    )
    assert (
        admin_client.get(
            reverse("admin:webhook_outbox_notificationoutbox_change", args=[entry.pk])
        ).status_code
        == 200
    )


def test_outbox_permissions(rf):
    model_admin = site._registry[NotificationOutbox]
    request = rf.get("/")
    request.user = User.objects.create_superuser("root", "root@example.com", "pw")
    entry = create_outbox_entry(create_tracked_table(Company))

    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request, entry) is True
    entry.status = Status.SENT
    assert model_admin.has_change_permission(request, entry) is False


def test_add_tracked_table_through_admin(admin_client):
    channel = create_channel()
    response = admin_client.post(
        reverse("admin:webhook_outbox_trackedtable_add"),
        _tracked_form_data(ct(RegistrationRequest), channel),
    )
    assert response.status_code == 302, response.content
    assert TrackedTable.objects.get().content_type == ct(RegistrationRequest)


# --- actions --------------------------------------------------------------------


def _run_action(admin_client, action, entries):
    return admin_client.post(
        reverse("admin:webhook_outbox_notificationoutbox_changelist"),
        {"action": action, "_selected_action": [e.pk for e in entries]},
        follow=True,
    )


def _entries():
    tracked = create_tracked_table(Company)
    return {
        "failed": create_outbox_entry(
            tracked, status=Status.FAILED, retry_count=3, error_message="HTTP 500"
        ),
        "pending": create_outbox_entry(tracked, retry_count=1, error_message="x"),
        "sent": create_outbox_entry(
            tracked, status=Status.SENT, sent_at=timezone.now()
        ),
    }


def test_retry_failed_resets_only_failed(admin_client):
    entries = _entries()

    response = _run_action(admin_client, "retry_failed", entries.values())

    assert "Queued 1 notification(s) for retry." in response.content.decode()
    for entry in entries.values():
        entry.refresh_from_db()
    assert (entries["failed"].status, entries["failed"].retry_count) == (
        Status.PENDING,
        0,
    )
    assert entries["failed"].error_message == ""
    assert (entries["pending"].retry_count, entries["pending"].error_message) == (
        1,
        "x",
    )
    assert entries["sent"].status == Status.SENT
    assert test_channels.SENT == []


@override_settings(WEBHOOK_OUTBOX={"ADMIN_RETRY_HOOK": "tests.channels.retry_hook"})
def test_retry_failed_calls_hook(admin_client):
    entries = _entries()
    _run_action(admin_client, "retry_failed", entries.values())
    assert [("hook", entries["failed"].pk)] == test_channels.SENT


@override_settings(WEBHOOK_OUTBOX={"ADMIN_RETRY_HOOK": "tests.channels.broken_hook"})
def test_retry_failed_survives_broken_hook(admin_client):
    entries = _entries()
    response = _run_action(admin_client, "retry_failed", entries.values())
    content = response.content.decode()
    assert "Queued 1 notification(s) for retry." in content
    assert "Immediate delivery could not be started for 1 notification(s)" in content
    entries["failed"].refresh_from_db()
    assert entries["failed"].status == Status.PENDING


@override_settings(
    WEBHOOK_OUTBOX={"ADMIN_RETRY_HOOK": "webhook_outbox.runner.deliver_entry"}
)
def test_retry_failed_with_synchronous_delivery(admin_client, mock_transport):
    recorder = mock_transport()
    entries = _entries()
    _run_action(admin_client, "retry_failed", entries.values())
    entries["failed"].refresh_from_db()
    assert entries["failed"].status == Status.SENT
    assert len(recorder.requests) == 1


def test_mark_as_sent(admin_client):
    entries = _entries()
    sent_at = entries["sent"].sent_at

    response = _run_action(admin_client, "mark_as_sent", entries.values())

    assert "Marked 2 notification(s) as sent." in response.content.decode()
    for entry in entries.values():
        entry.refresh_from_db()
        assert entry.status == Status.SENT
    assert entries["failed"].sent_at is not None
    assert entries["sent"].sent_at == sent_at
    # like the original (queryset.update), the error and retry count are kept
    assert entries["failed"].error_message == "HTTP 500"
