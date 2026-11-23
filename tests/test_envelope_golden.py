"""
Golden tests pinning envelope v1, the JSON document POSTed by WebhookChannel.

If one of these fails, the wire format changed: that is a breaking change
for every receiver (see docs/envelope.md) and needs a new envelope version.
"""

import json
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import httpx
import pytest
import time_machine

from tests.factories import create_channel, create_company, create_tracked_table
from tests.testapp.models import Company, RegistrationRequest
from webhook_outbox import runner
from webhook_outbox.models import NotificationOutbox
from webhook_outbox.services.channels import ENVELOPE_VERSION, NotificationPayload

pytestmark = pytest.mark.django_db

OCCURRED_AT = datetime(2026, 8, 3, 10, 0, 0, 123456, tzinfo=dt_timezone.utc)

GOLDEN_DICT = {
    "event": "insert",
    "source_model": "testapp.company",
    "source_pk": "42",
    "tracked_table": "New companies",
    "message": 'Новая компания «Acme» "quoted"',
    "occurred_at": "2026-08-03T10:00:00.123456+00:00",
}

GOLDEN_KEYS = [
    "event",
    "source_model",
    "source_pk",
    "tracked_table",
    "message",
    "occurred_at",
]

# httpx >= 0.28 serialises json= compactly, UTF-8, non-ASCII kept as is.
GOLDEN_BODY_HTTPX_028 = (
    '{"event":"insert","source_model":"testapp.company","source_pk":"42",'
    '"tracked_table":"New companies",'
    '"message":"Новая компания «Acme» \\"quoted\\"",'
    '"occurred_at":"2026-08-03T10:00:00.123456+00:00"}'
).encode()

# httpx 0.27 used json.dumps() defaults: ", " / ": " separators, ASCII escapes.
GOLDEN_BODY_HTTPX_027 = (
    b'{"event": "insert", "source_model": "testapp.company", "source_pk": "42", '
    b'"tracked_table": "New companies", '
    b'"message": "\\u041d\\u043e\\u0432\\u0430\\u044f \\u043a\\u043e\\u043c\\u043f'
    b'\\u0430\\u043d\\u0438\\u044f \\u00abAcme\\u00bb \\"quoted\\"", '
    b'"occurred_at": "2026-08-03T10:00:00.123456+00:00"}'
)


def _httpx_version() -> tuple[int, int]:
    major, minor = httpx.__version__.split(".")[:2]
    return int(major), int(minor)


def golden_body() -> bytes:
    return (
        GOLDEN_BODY_HTTPX_028 if _httpx_version() >= (0, 28) else GOLDEN_BODY_HTTPX_027
    )


def test_envelope_version_constant():
    assert ENVELOPE_VERSION == 1


def test_as_dict_keys_and_order():
    payload = NotificationPayload(
        message=GOLDEN_DICT["message"],
        event_type="insert",
        source_model="testapp.company",
        source_pk="42",
        tracked_table="New companies",
        occurred_at=OCCURRED_AT,
    )

    result = payload.as_dict()

    assert result == GOLDEN_DICT
    assert list(result) == GOLDEN_KEYS
    assert all(isinstance(value, str) for value in result.values())


def test_wire_bytes(mock_transport):
    """The exact bytes and headers on the wire, end to end from an outbox entry."""
    recorder = mock_transport()
    tracked = create_tracked_table(
        Company,
        name="New companies",
        channel=create_channel(
            config={
                "url": "https://receiver.example.com/hooks/outbox",
                "headers": {"Authorization": "Bearer test-token"},
            }
        ),
    )
    with time_machine.travel(OCCURRED_AT, tick=False):
        entry = NotificationOutbox.objects.create(
            tracked_table=tracked,
            source_model=tracked.model_path,
            source_pk="42",
            event_type="insert",
            message=GOLDEN_DICT["message"],
        )
    assert entry.source_model == "testapp.company"

    assert runner.deliver_once() == {"sent": 1, "failed": 0}

    (request,) = recorder.requests
    assert request.method == "POST"
    assert str(request.url) == "https://receiver.example.com/hooks/outbox"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["authorization"] == "Bearer test-token"
    assert request.content == golden_body()


def test_source_model_is_lowercase_app_label_dot_model(mock_transport):
    """source_model comes from the content type, so it is always lowercase."""
    recorder = mock_transport()
    tracked = create_tracked_table(RegistrationRequest)
    NotificationOutbox.objects.create(
        tracked_table=tracked,
        source_model=tracked.model_path,
        source_pk="7",
        event_type="insert",
        message="m",
    )

    runner.deliver_once()

    body = json.loads(recorder.requests[0].content)
    assert body["source_model"] == "testapp.registrationrequest"
    assert body["source_pk"] == "7"


def test_poller_queues_source_pk_as_string(mock_transport):
    recorder = mock_transport()
    tracked = create_tracked_table(Company, message_template="{company_name}")
    with time_machine.travel(OCCURRED_AT, tick=False):
        runner.poll_once()  # first poll: cursor only
    with time_machine.travel(OCCURRED_AT + timedelta(seconds=5), tick=False):
        company = create_company("Envelope Co")
    with time_machine.travel(OCCURRED_AT + timedelta(seconds=10), tick=False):
        assert runner.poll_once() == 1
        runner.deliver_once()

    body = json.loads(recorder.requests[0].content)
    assert body["source_pk"] == str(company.pk)
    assert body["message"] == "Envelope Co"
    assert body["tracked_table"] == tracked.name
    assert body["occurred_at"] == "2026-08-03T10:00:10.123456+00:00"
