"""webhook_outbox.enqueue(): explicit, transaction-safe events (#4)."""

import json
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import pytest
import time_machine
from django.db import transaction
from django.test import override_settings

from tests import channels as test_channels
from tests.factories import create_channel, create_company, create_tracked_table
from tests.testapp.models import Company, PlainItem
from webhook_outbox import TrackedTableLookupError, enqueue
from webhook_outbox.models import NotificationOutbox
from webhook_outbox.services.channels import NotificationPayload
from webhook_outbox.services.poller import poll_table

pytestmark = pytest.mark.django_db

V2_CONFIG = {"url": "https://example.com/hook", "envelope_version": 2}
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt_timezone.utc)


def at(seconds: float):
    return time_machine.travel(T0 + timedelta(seconds=seconds), tick=False)


@pytest.fixture(autouse=True)
def _clear_hook_calls():
    test_channels.ENQUEUED.clear()


# --- transactions -----------------------------------------------------------------


def test_rolled_back_transaction_creates_no_row():
    create_tracked_table(Company, "insert")
    with pytest.raises(RuntimeError), transaction.atomic():
        company = create_company("Rolled back")
        assert enqueue(company, event_type="insert") is not None
        assert NotificationOutbox.objects.count() == 1  # visible inside
        raise RuntimeError("business error")

    assert NotificationOutbox.objects.count() == 0
    assert Company.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_committed_transaction_creates_the_row():
    tracked = create_tracked_table(Company, "insert", message_template="{company_name}")
    with transaction.atomic():
        company = create_company("Committed")
        entry = enqueue(company, event_type="insert")

    entry.refresh_from_db()
    assert entry.tracked_table == tracked
    assert entry.source_model == "testapp.company"
    assert entry.source_pk == str(company.pk)
    assert entry.event_type == "insert"
    assert entry.message == "Committed"
    assert entry.status == NotificationOutbox.Status.PENDING
    assert entry.event_id is not None
    assert entry.source_changed_at == company.created_at


@pytest.mark.django_db(transaction=True)
@override_settings(WEBHOOK_OUTBOX={"ENQUEUE_HOOK": "tests.channels.enqueue_hook"})
def test_hook_runs_on_commit_only():
    create_tracked_table(Company, "insert")
    with pytest.raises(RuntimeError), transaction.atomic():
        enqueue(create_company("Rolled back"), event_type="insert")
        raise RuntimeError
    assert test_channels.ENQUEUED == []

    with transaction.atomic():
        entry = enqueue(create_company("Committed"), event_type="insert")
        assert test_channels.ENQUEUED == []
    assert list(test_channels.ENQUEUED) == [entry.pk]


@override_settings(WEBHOOK_OUTBOX={"ENQUEUE_HOOK": "tests.channels.broken_hook"})
def test_failing_hook_is_logged(django_capture_on_commit_callbacks, caplog):
    create_tracked_table(Company, "insert")
    with django_capture_on_commit_callbacks(execute=True):
        entry = enqueue(create_company(), event_type="insert")
    assert NotificationOutbox.objects.filter(pk=entry.pk).exists()
    assert "ENQUEUE_HOOK failed" in caplog.text


# --- routing ----------------------------------------------------------------------


def test_no_tracked_table_raises():
    with pytest.raises(TrackedTableLookupError, match="No tracked table"):
        enqueue(create_company(), event_type="insert")


def test_several_active_tracked_tables_raise_unless_channel_is_given():
    a = create_tracked_table(Company, "update")
    b = create_tracked_table(Company, "update")
    company = create_company()

    with pytest.raises(TrackedTableLookupError, match="2 active tracked tables"):
        enqueue(company, event_type="update")

    assert enqueue(company, event_type="update", channel=b.channel).tracked_table == b
    assert (
        enqueue(company, event_type="update", channel=a.channel.pk).tracked_table == a
    )


def test_inactive_ones_do_not_make_it_ambiguous():
    active = create_tracked_table(Company, "update")
    create_tracked_table(Company, "update", is_active=False)
    create_tracked_table(Company, "update", channel=create_channel(is_active=False))

    entry = enqueue(create_company(), event_type="update")

    assert entry.tracked_table == active


@pytest.mark.parametrize("what", ["table", "channel"])
def test_inactive_tracked_table_or_channel_queues_nothing(what):
    if what == "table":
        create_tracked_table(Company, "insert", is_active=False)
    else:
        create_tracked_table(Company, "insert", channel=create_channel(is_active=False))

    assert enqueue(create_company(), event_type="insert") is None
    assert NotificationOutbox.objects.count() == 0


def test_explicit_tracked_table_and_custom_event_type():
    tracked = create_tracked_table(Company, "update", message_template="{company_name}")
    company = create_company("Paid Co")

    with pytest.raises(TrackedTableLookupError):
        enqueue(company, event_type="paid")

    entry = enqueue(company, event_type="paid", tracked_table=tracked)
    assert entry.event_type == "paid"
    assert entry.source_changed_at is None
    assert enqueue(company, event_type="paid", tracked_table=tracked.pk).pk != entry.pk


def test_tracked_table_of_another_model_or_channel_is_rejected():
    tracked = create_tracked_table(PlainItem, "insert")
    with pytest.raises(ValueError, match="tracks testapp.plainitem"):
        enqueue(create_company(), event_type="insert", tracked_table=tracked)

    tracked = create_tracked_table(Company, "insert")
    with pytest.raises(ValueError, match="does not belong"):
        enqueue(
            create_company(),
            event_type="insert",
            tracked_table=tracked,
            channel=create_channel(),
        )


@pytest.mark.parametrize("event_type", ["", "x" * 11, None, 5])
def test_invalid_event_type(event_type):
    tracked = create_tracked_table(Company)
    with pytest.raises(ValueError, match="event_type"):
        enqueue(create_company(), event_type=event_type, tracked_table=tracked)


# --- source and message -----------------------------------------------------------


def test_ref_by_label_or_model_renders_from_the_database():
    create_tracked_table(Company, "update", message_template="Now {company_name}")
    company = create_company("Loaded")

    by_label = enqueue(("testapp.company", company.pk), event_type="update")
    by_model = enqueue((Company, str(company.pk)), event_type="update")

    assert by_label.message == by_model.message == "Now Loaded"
    assert by_label.source_pk == by_model.source_pk == str(company.pk)
    # The row was loaded to render the message: its updated_at is recorded.
    assert by_label.source_changed_at == company.updated_at

    explicit = enqueue((Company, company.pk), event_type="update", message="m")
    assert explicit.source_changed_at is None  # not loaded


def test_ref_to_a_deleted_row_needs_a_message():
    create_tracked_table(Company, "delete")
    company = create_company()
    pk = company.pk
    company.delete()

    with pytest.raises(ValueError, match="pass message="):
        enqueue((Company, pk), event_type="delete")

    entry = enqueue((Company, pk), event_type="delete", message="Company removed")
    assert entry.message == "Company removed"
    assert entry.source_pk == str(pk)


@pytest.mark.parametrize(
    "source", [None, "testapp.company", ("testapp.nope", 1), ("testapp.company", None)]
)
def test_invalid_source(source):
    create_tracked_table(Company)
    with pytest.raises((TypeError, ValueError)):
        enqueue(source, event_type="insert")


def test_unsaved_instance_is_rejected():
    create_tracked_table(Company)
    with pytest.raises(ValueError, match="saved instance"):
        enqueue(Company(company_name="unsaved"), event_type="insert")


def test_explicit_message_wins():
    create_tracked_table(Company, message_template="{company_name}")
    entry = enqueue(create_company("x"), event_type="insert", message="Custom")
    assert entry.message == "Custom"


def test_dedup_window_does_not_apply():
    create_tracked_table(Company, "update")
    company = create_company()
    enqueue(company, event_type="update")
    enqueue(company, event_type="update")
    assert NotificationOutbox.objects.count() == 2


# --- changes / extra and the envelope ---------------------------------------------


def test_changes_and_extra_need_envelope_v2():
    create_tracked_table(Company, "update")
    company = create_company()
    for kwargs in (
        {"changes": {"company_name": {"old": "a", "new": "b"}}},
        {"extra": {"k": "v"}},
    ):
        with pytest.raises(ValueError, match="envelope v1"):
            enqueue(company, event_type="update", **kwargs)
    assert NotificationOutbox.objects.count() == 0


@pytest.mark.parametrize("where", ["channel", "setting"])
def test_changes_and_extra_with_v2(where):
    config = V2_CONFIG if where == "channel" else {"url": "https://example.com/hook"}
    settings = {} if where == "channel" else {"ENVELOPE_VERSION": 2}
    create_tracked_table(Company, "update", channel=create_channel(config=config))
    company = create_company()

    with override_settings(WEBHOOK_OUTBOX=settings):
        entry = enqueue(
            company,
            event_type="update",
            changes={"registration_number": {"old": "1", "new": "2"}},
            extra={"when": T0, "actor": "tests"},
        )
        entry.refresh_from_db()
        body = NotificationPayload.from_outbox(entry).as_dict(2)

    assert entry.changes == {"registration_number": {"old": "1", "new": "2"}}
    # DjangoJSONEncoder: datetimes are stored as ISO 8601 strings.
    assert entry.extra == {"when": "2026-01-01T12:00:00Z", "actor": "tests"}
    assert body["changes"] == entry.changes
    assert body["extra"] == entry.extra
    assert body["event_id"] == str(entry.event_id)
    json.dumps(body)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"changes": []}, "changes must be a dict"),
        ({"changes": {"f": "x"}}, "'old' and 'new'"),
        ({"changes": {"f": {"old": 1}}}, "'old' and 'new'"),
        ({"changes": {"f": {"old": 1, "new": 2, "x": 3}}}, "'old' and 'new'"),
        ({"changes": {1: {"old": 1, "new": 2}}}, "not a string"),
        ({"changes": {"f": {"old": object(), "new": 2}}}, "not JSON"),
        ({"extra": ["x"]}, "extra must be a dict"),
        ({"extra": {"x": object()}}, "not JSON"),
    ],
)
def test_invalid_changes_or_extra(kwargs, match):
    create_tracked_table(Company, "update", channel=create_channel(config=V2_CONFIG))
    with pytest.raises(ValueError, match=match):
        enqueue(create_company(), event_type="update", **kwargs)


# --- interplay with the poller ----------------------------------------------------


@pytest.mark.parametrize("event_type", ["insert", "update"])
@override_settings(WEBHOOK_OUTBOX={"DEDUP_WINDOW_SECONDS": 0})
def test_poller_does_not_queue_an_enqueued_change_again(event_type):
    tracked = create_tracked_table(Company, event_type)
    with at(-10):
        company = create_company("Old")
    with at(0):
        poll_table(tracked)
    with at(1):
        if event_type == "insert":
            company = create_company("New")
        else:
            company.company_name = "Changed"
            company.save()
        enqueue(company, event_type=event_type)
    for second in (2, 30, 70):
        with at(second):
            assert poll_table(tracked) == 0

    assert NotificationOutbox.objects.count() == 1


@override_settings(WEBHOOK_OUTBOX={"DEDUP_WINDOW_SECONDS": 0})
def test_poller_still_reports_a_later_change():
    tracked = create_tracked_table(Company, "update")
    with at(-10):
        company = create_company("Old")
    with at(0):
        poll_table(tracked)
    with at(1):
        company.save()
        enqueue(company, event_type="update")
    with at(5):
        company.save()  # not enqueued: the poller reports it
    with at(6):
        assert poll_table(tracked) == 1
    assert NotificationOutbox.objects.count() == 2
