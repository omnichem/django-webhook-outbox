"""
Golden tests pinning envelopes v1 and v2, the JSON documents POSTed by
WebhookChannel.

If one of these fails, the wire format changed: that is a breaking change
for every receiver (see docs/envelope.md) and needs a new envelope version.
The documents are also validated against the JSON schemas published in
docs/envelope.md.
"""

import json
import re
import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path

import httpx
import jsonschema
import pytest
import time_machine
from django.test import override_settings

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


# --- schemas published in docs/envelope.md ------------------------------------

DOCS = Path(__file__).resolve().parent.parent / "docs" / "envelope.md"


def published_schema(title: str) -> dict:
    blocks = re.findall(r"```json\n(.*?)```", DOCS.read_text(encoding="utf-8"), re.S)
    for block in blocks:
        document = json.loads(block)
        if document.get("title") == title:
            jsonschema.Draft202012Validator.check_schema(document)
            return document
    raise AssertionError(f"No JSON schema titled {title!r} in {DOCS}")


V1_SCHEMA_TITLE = "django-webhook-outbox envelope v1"
V2_SCHEMA_TITLE = "django-webhook-outbox envelope v2"

EVENT_ID = uuid.UUID("1b0e6f0a-3c4d-4e5f-8a9b-0c1d2e3f4a5b")


def golden_payload(**overrides) -> NotificationPayload:
    values = {
        "message": GOLDEN_DICT["message"],
        "event_type": "insert",
        "source_model": "testapp.company",
        "source_pk": "42",
        "tracked_table": "New companies",
        "occurred_at": OCCURRED_AT,
        "event_id": EVENT_ID,
    }
    values.update(overrides)
    return NotificationPayload(**values)


def test_default_payload_is_the_1_0_document():
    """With default settings nothing of 1.1 shows up in the document."""
    result = golden_payload(
        changes={"company_name": {"old": "a", "new": "b"}},
        extra={"k": "v"},
        source_changed_at=OCCURRED_AT - timedelta(seconds=5),
    ).as_dict()

    assert result == GOLDEN_DICT
    assert list(result) == GOLDEN_KEYS
    jsonschema.validate(result, published_schema(V1_SCHEMA_TITLE))


@override_settings(WEBHOOK_OUTBOX={"ENVELOPE_V1_EVENT_ID": True})
def test_v1_with_event_id():
    result = golden_payload().as_dict()

    assert list(result) == [*GOLDEN_KEYS, "event_id"]
    assert result == {**GOLDEN_DICT, "event_id": str(EVENT_ID)}
    jsonschema.validate(result, published_schema(V1_SCHEMA_TITLE))


GOLDEN_V2_DICT = {
    "schema_version": 2,
    "event_id": "1b0e6f0a-3c4d-4e5f-8a9b-0c1d2e3f4a5b",
    "event_type": "update",
    "occurred_at": "2026-08-03T09:59:55.123456+00:00",
    "source": {"model": "testapp.company", "pk": "42"},
    "tracked_table": "New companies",
    "message": GOLDEN_DICT["message"],
    "changes": {"company_name": {"old": "Old", "new": "Acme"}},
    "extra": {"actor": "tests", "count": 2},
}

GOLDEN_V2_KEYS = [
    "schema_version",
    "event_id",
    "event_type",
    "occurred_at",
    "source",
    "tracked_table",
    "message",
    "changes",
    "extra",
]


def test_v2_document():
    result = golden_payload(
        event_type="update",
        changes={"company_name": {"old": "Old", "new": "Acme"}},
        extra={"actor": "tests", "count": 2},
        source_changed_at=OCCURRED_AT - timedelta(seconds=5),
    ).as_dict(2)

    assert result == GOLDEN_V2_DICT
    assert list(result) == GOLDEN_V2_KEYS
    jsonschema.validate(result, published_schema(V2_SCHEMA_TITLE))


def test_v2_without_changes_extra_or_source_time():
    result = golden_payload().as_dict(2)

    assert result["changes"] is None
    assert result["extra"] is None
    assert result["occurred_at"] == GOLDEN_DICT["occurred_at"]
    jsonschema.validate(result, published_schema(V2_SCHEMA_TITLE))


@pytest.mark.parametrize(
    "document",
    [
        {**GOLDEN_V2_DICT, "unknown": 1},
        {**GOLDEN_V2_DICT, "schema_version": 1},
        {**GOLDEN_V2_DICT, "event_id": None},
        {**GOLDEN_V2_DICT, "changes": {"f": {"old": 1}}},
        {k: v for k, v in GOLDEN_V2_DICT.items() if k != "extra"},
    ],
)
def test_v2_schema_rejects(document):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, published_schema(V2_SCHEMA_TITLE))


def test_unknown_envelope_version():
    with pytest.raises(ValueError, match="Unsupported envelope version 3"):
        golden_payload().as_dict(3)


GOLDEN_V2_BODY_HTTPX_028 = (
    '{"schema_version":2,"event_id":"1b0e6f0a-3c4d-4e5f-8a9b-0c1d2e3f4a5b",'
    '"event_type":"insert","occurred_at":"2026-08-03T10:00:00.123456+00:00",'
    '"source":{"model":"testapp.company","pk":"42"},'
    '"tracked_table":"New companies",'
    '"message":"Новая компания «Acme» \\"quoted\\"",'
    '"changes":null,"extra":{"n":1}}'
).encode()


def _create_golden_entry(channel_config):
    tracked = create_tracked_table(
        Company,
        name="New companies",
        channel=create_channel(config=channel_config),
    )
    with time_machine.travel(OCCURRED_AT, tick=False):
        return NotificationOutbox.objects.create(
            tracked_table=tracked,
            source_model=tracked.model_path,
            source_pk="42",
            event_type="insert",
            message=GOLDEN_DICT["message"],
            event_id=EVENT_ID,
            extra={"n": 1},
        )


@pytest.mark.parametrize("where", ["channel", "setting"])
def test_v2_wire_bytes(mock_transport, where):
    recorder = mock_transport()
    config = {"url": "https://receiver.example.com/hooks/outbox"}
    settings = {}
    if where == "channel":
        config["envelope_version"] = 2
    else:
        settings["ENVELOPE_VERSION"] = 2
    _create_golden_entry(config)

    with override_settings(WEBHOOK_OUTBOX=settings):
        assert runner.deliver_once() == {"sent": 1, "failed": 0}

    (request,) = recorder.requests
    body = json.loads(request.content)
    jsonschema.validate(body, published_schema(V2_SCHEMA_TITLE))
    assert list(body) == GOLDEN_V2_KEYS
    if _httpx_version() >= (0, 28):
        assert request.content == GOLDEN_V2_BODY_HTTPX_028


@override_settings(WEBHOOK_OUTBOX={"ENVELOPE_VERSION": 2})
def test_channel_config_can_pin_v1(mock_transport):
    recorder = mock_transport()
    _create_golden_entry(
        {"url": "https://receiver.example.com/hooks/outbox", "envelope_version": 1}
    )

    runner.deliver_once()

    assert recorder.requests[0].content == golden_body()


@override_settings(WEBHOOK_OUTBOX={"ENVELOPE_VERSION": 2, "MAX_RETRIES": 3})
def test_event_id_is_stable_across_retries(mock_transport):
    responses = iter([httpx.Response(500), httpx.Response(503), httpx.Response(200)])
    recorder = mock_transport(lambda request: next(responses))
    tracked = create_tracked_table(Company)
    entry = NotificationOutbox.objects.create(
        tracked_table=tracked,
        source_model=tracked.model_path,
        source_pk="1",
        event_type="insert",
        message="m",
    )

    runner.deliver_once()
    runner.deliver_once()
    runner.deliver_entry(entry.pk)

    ids = {json.loads(r.content)["event_id"] for r in recorder.requests}
    assert len(recorder.requests) == 3
    assert ids == {str(entry.event_id)}
    entry.refresh_from_db()
    assert entry.status == NotificationOutbox.Status.SENT


@override_settings(WEBHOOK_OUTBOX={"ENVELOPE_V1_EVENT_ID": True})
def test_entry_from_1_0_gets_a_persistent_event_id(mock_transport):
    recorder = mock_transport(lambda request: httpx.Response(500))
    tracked = create_tracked_table(Company)
    entry = NotificationOutbox.objects.create(
        tracked_table=tracked,
        source_model=tracked.model_path,
        source_pk="1",
        event_type="insert",
        message="m",
    )
    NotificationOutbox.objects.filter(pk=entry.pk).update(event_id=None)

    runner.deliver_once()
    runner.deliver_once()

    entry.refresh_from_db()
    assert entry.event_id is not None
    assert [json.loads(r.content)["event_id"] for r in recorder.requests] == [
        str(entry.event_id)
    ] * 2


def test_poller_entries_carry_source_time_into_v2(mock_transport):
    recorder = mock_transport()
    create_tracked_table(
        Company,
        message_template="{company_name}",
        channel=create_channel(
            config={"url": "https://example.com/hook", "envelope_version": 2}
        ),
    )
    with time_machine.travel(OCCURRED_AT, tick=False):
        runner.poll_once()
    with time_machine.travel(OCCURRED_AT + timedelta(seconds=5), tick=False):
        create_company("Envelope Co")
    with time_machine.travel(OCCURRED_AT + timedelta(seconds=10), tick=False):
        assert runner.poll_once() == 1
        runner.deliver_once()

    body = json.loads(recorder.requests[0].content)
    jsonschema.validate(body, published_schema(V2_SCHEMA_TITLE))
    # v2 occurred_at is the row's created_at, not the time it was queued.
    assert body["occurred_at"] == "2026-08-03T10:00:05.123456+00:00"
    assert body["changes"] is None
