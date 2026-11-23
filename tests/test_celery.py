import httpx
import pytest

pytest.importorskip("celery")

from tests.factories import create_outbox_entry, create_tracked_table  # noqa: E402
from tests.testapp.models import Company  # noqa: E402
from webhook_outbox.contrib.celery import tasks  # noqa: E402
from webhook_outbox.contrib.celery.schedule import beat_schedule  # noqa: E402
from webhook_outbox.models import NotificationOutbox  # noqa: E402

pytestmark = pytest.mark.django_db


def test_task_names():
    assert tasks.poll_tracked_tables.name == "webhook_outbox.poll_tracked_tables"
    assert tasks.process_outbox.name == "webhook_outbox.process_outbox"
    assert (
        tasks.send_single_notification.name == "webhook_outbox.send_single_notification"
    )
    assert tasks.poll_tracked_tables.max_retries == 3
    assert tasks.poll_tracked_tables.default_retry_delay == 60
    assert tasks.process_outbox.default_retry_delay == 30
    assert tasks.send_single_notification.default_retry_delay == 10


def test_poll_task(monkeypatch):
    monkeypatch.setattr("webhook_outbox.runner.poll_all_tables", lambda: 5)
    assert tasks.poll_tracked_tables() == {"queued": 5}


def test_poll_task_error_retries(monkeypatch):
    def boom():
        raise Exception("Database error")

    monkeypatch.setattr("webhook_outbox.runner.poll_all_tables", boom)
    with pytest.raises(Exception, match="Database error"):
        tasks.poll_tracked_tables()


def test_process_outbox_task(mock_transport):
    mock_transport()
    create_outbox_entry(create_tracked_table(Company))
    assert tasks.process_outbox() == {"sent": 1, "failed": 0}


def test_send_single_notification_task(mock_transport):
    mock_transport()
    entry = create_outbox_entry(create_tracked_table(Company))
    assert tasks.send_single_notification(entry.pk) == {"success": True, "error": ""}
    assert tasks.send_single_notification(entry.pk) == {"status": "already_sent"}
    assert tasks.send_single_notification(10**9) == {"error": "Entry not found"}


def test_send_single_notification_task_retries_and_keeps_attempt(mock_transport):
    mock_transport(lambda request: httpx.Response(503, text="busy"))
    entry = create_outbox_entry(create_tracked_table(Company))

    # Called directly, Task.retry() re-raises the given exception.
    with pytest.raises(Exception, match="HTTP 503: busy"):
        tasks.send_single_notification(entry.pk)

    entry.refresh_from_db()
    assert (entry.status, entry.retry_count) == (NotificationOutbox.Status.PENDING, 1)


def test_send_single_notification_task_final_failure(mock_transport):
    mock_transport(lambda request: httpx.Response(503, text="busy"))
    entry = create_outbox_entry(create_tracked_table(Company), retry_count=2)

    assert tasks.send_single_notification(entry.pk) == {
        "success": False,
        "error": "HTTP 503: busy",
    }
    entry.refresh_from_db()
    assert entry.status == NotificationOutbox.Status.FAILED


def test_enqueue_hook(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks.send_single_notification, "delay", calls.append)
    tasks.enqueue_single_notification(7)
    assert calls == [7]


def test_beat_schedule():
    assert beat_schedule() == {
        "webhook-outbox-poll-tracked-tables": {
            "task": "webhook_outbox.poll_tracked_tables",
            "schedule": 30.0,
        },
        "webhook-outbox-process-outbox": {
            "task": "webhook_outbox.process_outbox",
            "schedule": 10.0,
        },
    }
    assert beat_schedule(60, 5)["webhook-outbox-process-outbox"]["schedule"] == 5.0
