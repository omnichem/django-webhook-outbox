"""
Delivery state machine, end to end through httpx.MockTransport (no network):

    PENDING --2xx--> SENT
    PENDING --fail--> PENDING (retry_count + 1) ... --> FAILED at MAX_RETRIES
"""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import httpx
import pytest
import time_machine
from django.test import override_settings

from tests.factories import create_channel, create_outbox_entry, create_tracked_table
from tests.testapp.models import Company
from webhook_outbox import runner
from webhook_outbox.models import NotificationOutbox

pytestmark = pytest.mark.django_db

T0 = datetime(2026, 3, 1, 9, 0, 0, tzinfo=dt_timezone.utc)
Status = NotificationOutbox.Status


def at(seconds: float):
    return time_machine.travel(T0 + timedelta(seconds=seconds), tick=False)


@pytest.fixture
def entry():
    tracked = create_tracked_table(Company)
    with at(0):
        return create_outbox_entry(tracked)


def responses(*items):
    """Handler returning the given responses (or raising exceptions) in order."""
    queue = list(items)

    def handler(request):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return handler


def snapshot(entry):
    entry.refresh_from_db()
    return entry.status, entry.retry_count, entry.error_message


def test_success_marks_sent(mock_transport, entry):
    mock_transport(responses(httpx.Response(204)))

    with at(10):
        assert runner.deliver_once() == {"sent": 1, "failed": 0}

    assert snapshot(entry) == (Status.SENT, 0, "")
    assert entry.sent_at == T0 + timedelta(seconds=10)
    assert entry.updated_at == T0 + timedelta(seconds=10)


@pytest.mark.parametrize("status_code", [200, 201, 202, 204, 299])
def test_any_2xx_is_success(mock_transport, entry, status_code):
    mock_transport(responses(httpx.Response(status_code)))
    runner.deliver_once()
    assert snapshot(entry)[0] == Status.SENT


def test_three_failures_mark_failed(mock_transport, entry):
    recorder = mock_transport(
        responses(*[httpx.Response(503, text="down") for _ in range(3)])
    )

    with at(10):
        assert runner.deliver_once() == {"sent": 0, "failed": 0}
    assert snapshot(entry) == (Status.PENDING, 1, "HTTP 503: down")
    assert entry.updated_at == T0 + timedelta(seconds=10)

    with at(20):
        assert runner.deliver_once() == {"sent": 0, "failed": 0}
    assert snapshot(entry) == (Status.PENDING, 2, "HTTP 503: down")

    with at(30):
        assert runner.deliver_once() == {"sent": 0, "failed": 1}
    assert snapshot(entry) == (Status.FAILED, 3, "HTTP 503: down")
    assert entry.sent_at is None

    with at(40):
        assert runner.deliver_once() == {"sent": 0, "failed": 0}
    assert len(recorder.requests) == 3


def test_failure_then_success(mock_transport, entry):
    mock_transport(responses(httpx.Response(500, text="oops"), httpx.Response(200)))

    with at(10):
        runner.deliver_once()
    assert snapshot(entry) == (Status.PENDING, 1, "HTTP 500: oops")

    with at(20):
        runner.deliver_once()
    assert snapshot(entry) == (Status.SENT, 1, "")
    assert entry.sent_at == T0 + timedelta(seconds=20)


@override_settings(WEBHOOK_OUTBOX={"MAX_RETRIES": 1}, NOTIFICATION_MAX_RETRIES=5)
def test_max_retries_setting_wins_over_legacy(mock_transport, entry):
    mock_transport(responses(httpx.Response(500)))
    assert runner.deliver_once() == {"sent": 0, "failed": 1}
    assert snapshot(entry)[:2] == (Status.FAILED, 1)


@override_settings(NOTIFICATION_MAX_RETRIES=1)
def test_legacy_max_retries(mock_transport, entry):
    mock_transport(responses(httpx.Response(500)))
    runner.deliver_once()
    assert snapshot(entry)[:2] == (Status.FAILED, 1)


def test_redirect_is_not_followed(mock_transport, entry):
    recorder = mock_transport(
        responses(
            httpx.Response(302, headers={"Location": "https://elsewhere.example.com/"})
        )
    )
    runner.deliver_once()
    assert snapshot(entry) == (Status.PENDING, 1, "HTTP 302: ")
    assert len(recorder.requests) == 1


def test_timeout_error_message(mock_transport, entry):
    mock_transport(responses(httpx.ReadTimeout("slow")))
    runner.deliver_once()
    assert snapshot(entry) == (Status.PENDING, 1, "Request timed out")


def test_connection_error_message(mock_transport, entry):
    mock_transport(responses(httpx.ConnectError("connection refused")))
    runner.deliver_once()
    assert snapshot(entry) == (Status.PENDING, 1, "Request error: connection refused")


def test_error_body_is_truncated(mock_transport, entry):
    mock_transport(responses(httpx.Response(500, text="é" * 2000)))
    runner.deliver_once()
    assert snapshot(entry)[2] == "HTTP 500: " + "é" * 500


def test_inactive_channel_counts_as_failed_attempt(mock_transport):
    """Entries already queued for a channel that is then deactivated burn retries."""
    recorder = mock_transport()
    channel = create_channel()
    entry = create_outbox_entry(create_tracked_table(Company, channel=channel))
    channel.is_active = False
    channel.save()

    for _ in range(3):
        runner.deliver_once()

    assert snapshot(entry) == (Status.FAILED, 3, "Channel is inactive")
    assert recorder.requests == []


def test_invalid_config_counts_as_failed_attempt(mock_transport):
    recorder = mock_transport()
    channel = create_channel(config={"url": "ftp://example.com"})
    entry = create_outbox_entry(create_tracked_table(Company, channel=channel))

    runner.deliver_once()

    status, retry_count, error = snapshot(entry)
    assert (status, retry_count) == (Status.PENDING, 1)
    assert error.startswith("Invalid webhook url: ftp://example.com.")
    assert recorder.requests == []


def test_channel_timeout_is_passed_to_httpx(mock_transport):
    recorder = mock_transport()
    channel = create_channel(config={"url": "https://example.com/hook", "timeout": 2.5})
    create_outbox_entry(create_tracked_table(Company, channel=channel))

    runner.deliver_once()

    assert recorder.client_kwargs == [{"timeout": 2.5}]


@override_settings(WEBHOOK_OUTBOX={"WEBHOOK_TIMEOUT": 7})
def test_default_timeout_setting(mock_transport, entry):
    recorder = mock_transport()
    runner.deliver_once()
    assert recorder.client_kwargs == [{"timeout": 7}]


def test_default_timeout_is_30(mock_transport, entry):
    recorder = mock_transport()
    runner.deliver_once()
    assert recorder.client_kwargs == [{"timeout": 30}]


def test_deliver_entry(mock_transport, entry):
    mock_transport(responses(httpx.Response(500, text="x"), httpx.Response(200)))

    assert runner.deliver_entry(entry.pk) == {"success": False, "error": "HTTP 500: x"}
    assert snapshot(entry) == (Status.PENDING, 1, "HTTP 500: x")

    assert runner.deliver_entry(entry.pk) == {"success": True, "error": ""}
    assert snapshot(entry) == (Status.SENT, 1, "")

    assert runner.deliver_entry(entry.pk) == {"status": "already_sent"}
