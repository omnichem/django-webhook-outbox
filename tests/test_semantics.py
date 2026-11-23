"""
Tracking semantics, pinned with a frozen clock.

Several tests here pin *limitations* of the original implementation that are
still there (docs/semantics.md, "Known limitations"). They are named
``test_limitation_*``: if one starts failing, behaviour changed and the docs
and changelog must say so. Limitations fixed in 1.1 (lost events, late
commits, two pollers) are pinned by ``test_fixed_*`` tests instead.

The default POLL_OVERLAP_SECONDS is 60: the cursor trails the poll time by a
minute. Tests that pin the 1.0 cursor use ``NO_OVERLAP``.
"""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

import pytest
import time_machine
from django.test import override_settings

from tests.factories import (
    create_channel,
    create_company,
    create_request,
    create_tracked_table,
)
from tests.testapp.models import (
    Company,
    CustomManagerItem,
    InsertOnlyItem,
    PlainItem,
    RegistrationRequest,
    SoftDeletedItem,
)
from webhook_outbox.models import NotificationOutbox, TrackedTable
from webhook_outbox.services.channels import NotificationPayload
from webhook_outbox.services.poller import poll_all_tables, poll_table
from webhook_outbox.services.template import render_template

pytestmark = pytest.mark.django_db

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt_timezone.utc)

NO_OVERLAP = override_settings(WEBHOOK_OUTBOX={"POLL_OVERLAP_SECONDS": 0})


def at(seconds: float):
    return time_machine.travel(T0 + timedelta(seconds=seconds), tick=False)


def entries(tracked=None, **filters):
    qs = NotificationOutbox.objects.order_by("created_at", "pk")
    if tracked is not None:
        qs = qs.filter(tracked_table=tracked)
    return list(qs.filter(**filters))


def reload(tracked: TrackedTable) -> TrackedTable:
    tracked.refresh_from_db()
    return tracked


# --- first poll ---------------------------------------------------------------


def test_first_poll_sets_cursor_without_backfill():
    with at(-60):
        create_company("Existing before tracking")
    tracked = create_tracked_table(Company, message_template="{company_name}")

    with at(0):
        assert poll_table(tracked) == 0

    assert reload(tracked).last_polled_at == T0
    assert entries() == []

    with at(5):
        create_company("New one")
    with at(10):
        assert poll_table(tracked) == 1

    assert [e.message for e in entries()] == ["New one"]
    # The cursor trails the poll by POLL_OVERLAP_SECONDS (60) and never moves
    # backwards: it stays at T0 for the first minute.
    assert reload(tracked).last_polled_at == T0
    with at(100):
        assert poll_table(tracked) == 0
    assert reload(tracked).last_polled_at == T0 + timedelta(seconds=40)
    assert len(entries()) == 1


@NO_OVERLAP
def test_first_poll_without_overlap_moves_cursor_to_poll_time():
    tracked = create_tracked_table(Company, message_template="{company_name}")
    with at(0):
        poll_table(tracked)
    with at(5):
        create_company("New one")
    with at(10):
        assert poll_table(tracked) == 1
    assert reload(tracked).last_polled_at == T0 + timedelta(seconds=10)


def test_poll_all_tables_first_poll_initialises_every_table():
    insert = create_tracked_table(Company, "insert")
    update = create_tracked_table(Company, "update", channel=insert.channel)

    with at(0):
        assert poll_all_tables() == 0

    assert reload(insert).last_polled_at == T0
    assert reload(update).last_polled_at == T0


# --- INSERT / UPDATE split ----------------------------------------------------


def test_insert_update_split():
    channel = create_channel()
    insert = create_tracked_table(
        Company, "insert", channel=channel, message_template="ins {company_name}"
    )
    update = create_tracked_table(
        Company, "update", channel=channel, message_template="upd {company_name}"
    )
    with at(0):
        poll_all_tables()

    with at(1):
        company = create_company("A")
    with at(2):
        assert poll_all_tables() == 1
    assert [(e.event_type, e.message) for e in entries()] == [("insert", "ins A")]

    with at(3):
        company.company_name = "A2"
        company.save()
    with at(4):
        assert poll_all_tables() == 1
    assert [(e.event_type, e.message) for e in entries(update)] == [
        ("update", "upd A2")
    ]
    assert len(entries(insert)) == 1


def test_insert_filter_is_strictly_after_cursor():
    tracked = create_tracked_table(Company)
    with at(0):
        poll_table(tracked)
    with at(-1):
        create_company("Older than the cursor")
    with at(1):
        create_company("Newer")
    with at(2):
        assert poll_table(tracked) == 1


def test_update_filter_requires_created_before_cursor_and_updated_after():
    tracked = create_tracked_table(Company, "update")
    with at(-10):
        old = create_company("Old")
        untouched = create_company("Untouched")
    with at(0):
        poll_table(tracked)
    with at(1):
        old.save()
    with at(2):
        assert poll_table(tracked) == 1
    assert [e.source_pk for e in entries()] == [str(old.pk)]
    assert str(untouched.pk) not in {e.source_pk for e in entries()}


def test_limitation_created_and_updated_in_one_window_collapses_to_insert():
    channel = create_channel()
    insert = create_tracked_table(Company, "insert", channel=channel)
    update = create_tracked_table(Company, "update", channel=channel)
    with at(0):
        poll_all_tables()
    with at(1):
        company = create_company("B")
    with at(2):
        company.save()
        company.save()
    with at(3):
        assert poll_all_tables() == 1

    assert len(entries(insert)) == 1
    assert entries(update) == []


def test_limitation_several_updates_in_one_window_are_one_event():
    tracked = create_tracked_table(Company, "update")
    with at(-10):
        company = create_company("C")
    with at(0):
        poll_table(tracked)
    for second in (1, 2, 3):
        with at(second):
            company.company_name = f"C{second}"
            company.save()
    with at(4):
        assert poll_table(tracked) == 1
    # The message renders the row as it is at poll time; no old/new values.
    assert entries()[0].message == "Test"


def test_limitation_row_created_exactly_at_cursor_is_never_reported():
    channel = create_channel()
    insert = create_tracked_table(Company, "insert", channel=channel)
    update = create_tracked_table(Company, "update", channel=channel)
    with at(0):
        poll_all_tables()
        create_company("Same microsecond as the cursor")
    with at(1):
        assert poll_all_tables() == 0
    assert entries(insert) == entries(update) == []


def test_limitation_no_event_id_or_old_values():
    """The envelope has exactly these keys: no event id, no old/new values."""
    tracked = create_tracked_table(Company, "update")
    with at(-10):
        company = create_company("Before")
    with at(0):
        poll_table(tracked)
    with at(1):
        company.company_name = "After"
        company.save()
    with at(2):
        assert poll_table(tracked) == 1

    payload = NotificationPayload.from_outbox(entries()[0])

    assert set(payload.as_dict()) == {
        "event",
        "source_model",
        "source_pk",
        "tracked_table",
        "message",
        "occurred_at",
    }


# --- dedup window -------------------------------------------------------------


def _updates_polled_at(tracked, company, seconds):
    for second in seconds:
        with at(second - 0.5):
            company.save()
        with at(second):
            poll_table(tracked)


def test_dedup_window_default_is_five_minutes():
    tracked = create_tracked_table(Company, "update")
    with at(-10):
        company = create_company("D")
    with at(0):
        poll_table(tracked)

    # entry at t=10; t=310 is exactly 300s later (still a duplicate); t=311 is not
    _updates_polled_at(tracked, company, [10, 60, 310, 311])

    assert [e.created_at for e in entries()] == [
        T0 + timedelta(seconds=10),
        T0 + timedelta(seconds=311),
    ]


@override_settings(WEBHOOK_OUTBOX={"DEDUP_WINDOW_SECONDS": 20})
def test_dedup_window_setting():
    tracked = create_tracked_table(Company, "update")
    with at(-10):
        company = create_company("E")
    with at(0):
        poll_table(tracked)

    _updates_polled_at(tracked, company, [10, 25, 31])

    assert [e.created_at for e in entries()] == [
        T0 + timedelta(seconds=10),
        T0 + timedelta(seconds=31),
    ]


def test_dedup_is_per_tracked_table_pk_and_event_type():
    channel = create_channel()
    other_channel = create_channel()
    a = create_tracked_table(Company, "insert", channel=channel)
    b = create_tracked_table(Company, "insert", channel=other_channel)
    with at(0):
        poll_all_tables()
    with at(1):
        create_company("F")
        create_company("G")
    with at(2):
        assert poll_all_tables() == 4
    assert len(entries(a)) == len(entries(b)) == 2


def test_limitation_dedup_suppresses_a_legitimate_second_update():
    tracked = create_tracked_table(Company, "update")
    with at(-10):
        company = create_company("H")
    with at(0):
        poll_table(tracked)

    _updates_polled_at(tracked, company, [30, 90])

    # The second, real change at t=89.5 is dropped: same row, within 300s.
    assert len(entries()) == 1


@pytest.mark.parametrize("overlap", [0, 60])
def test_fixed_two_pollers_with_a_stale_cursor_do_not_duplicate(overlap):
    """
    1.0 queued the change twice. Now the cursor is re-read under a row lock
    and the change is recognised by its row timestamp, even with the dedup
    window off.
    """
    settings = {"DEDUP_WINDOW_SECONDS": 0, "POLL_OVERLAP_SECONDS": overlap}
    with override_settings(WEBHOOK_OUTBOX=settings):
        tracked = create_tracked_table(Company)
        with at(0):
            poll_table(tracked)
        with at(1):
            create_company("Once")

        other_poller = TrackedTable.objects.get(pk=tracked.pk)  # same cursor
        with at(2):
            assert poll_table(tracked) == 1
        with at(3):
            assert poll_table(other_poller) == 0

    assert len(entries()) == 1


@pytest.mark.django_db(transaction=True)
def test_poll_locks_the_tracked_table_row():
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    if not connection.features.has_select_for_update:
        pytest.skip("the database has no SELECT ... FOR UPDATE")
    tracked = create_tracked_table(Company)
    with CaptureQueriesContext(connection) as queries:
        poll_table(tracked)
    table = TrackedTable._meta.db_table
    assert any(
        "FOR UPDATE" in q["sql"] and table in q["sql"] for q in queries.captured_queries
    )


# --- cursor / transactions ----------------------------------------------------


@pytest.mark.parametrize("overlap", [0, 60])
def test_fixed_failed_outbox_insert_keeps_cursor_and_next_poll_emits(overlap):
    """1.0 moved the cursor first: an insert that failed lost the event."""
    with override_settings(WEBHOOK_OUTBOX={"POLL_OVERLAP_SECONDS": overlap}):
        tracked = create_tracked_table(Company)
        with at(0):
            poll_table(tracked)
        with at(1):
            create_company("Not lost")

        with (
            at(200),
            patch.object(
                NotificationOutbox.objects,
                "create",
                side_effect=RuntimeError("db hiccup"),
            ),
        ):
            assert poll_table(tracked) == 0
        assert reload(tracked).last_polled_at == T0
        assert entries() == []

        with at(201):
            assert poll_table(tracked) == 1
        assert [e.source_pk for e in entries()] == [str(Company.objects.get().pk)]
        assert reload(tracked).last_polled_at == T0 + timedelta(seconds=201 - overlap)


def test_fixed_error_during_detection_rolls_back_cursor():
    tracked = create_tracked_table(Company)
    with at(0):
        poll_table(tracked)
    with at(1):
        create_company("Still there")
    with (
        at(200),
        patch(
            "webhook_outbox.services.poller.TablePoller.detect",
            side_effect=RuntimeError("query failed"),
        ),
        pytest.raises(RuntimeError),
    ):
        poll_table(tracked)
    assert reload(tracked).last_polled_at == T0
    with at(201):
        assert poll_table(tracked) == 1


def test_fixed_one_failed_row_keeps_cursor_other_rows_are_queued_once():
    tracked = create_tracked_table(
        Company,
        message_template="{company_name}",
        dedup_window_seconds=0,
    )
    with at(0):
        poll_table(tracked)
    with at(1):
        create_company("One")
        create_company("Two")

    def flaky(template, instance):
        if instance.company_name == "One":
            raise RuntimeError("boom")
        return render_template(template, instance)

    with (
        at(200),
        patch("webhook_outbox.services.poller.render_template", side_effect=flaky),
    ):
        assert poll_table(tracked) == 1
    assert reload(tracked).last_polled_at == T0

    # "Two" is re-read but recognised by its created_at; "One" is retried.
    with at(201):
        assert poll_table(tracked) == 1
    assert sorted(e.message for e in entries()) == ["One", "Two"]


@override_settings(WEBHOOK_OUTBOX={"DEDUP_WINDOW_SECONDS": 0})
def test_fixed_late_commit_is_emitted_exactly_once():
    """
    A row saved at t=5 in a transaction that commits after the poll at t=10:
    its created_at is older than that poll. The cursor trails the polls by
    POLL_OVERLAP_SECONDS, so a later poll still sees the row, and the overlap
    re-reads do not queue it again (exact dedup, dedup window off).
    """
    tracked = create_tracked_table(Company)
    with at(0):
        poll_table(tracked)
    with at(10):
        assert poll_table(tracked) == 0
    # Simulates a transaction that started at t=5 and committed after the t=10 poll.
    with at(5):
        create_company("Committed late")

    queued = []
    for second in (20, 30, 40, 50, 60, 70, 80, 120):
        with at(second):
            queued.append(poll_table(tracked))

    assert queued == [1, 0, 0, 0, 0, 0, 0, 0]
    assert len(entries()) == 1
    # The cursor has moved past the row: it is not read again.
    assert reload(tracked).last_polled_at == T0 + timedelta(seconds=60)


def test_fixed_late_commit_of_an_update_is_emitted():
    tracked = create_tracked_table(Company, "update", dedup_window_seconds=0)
    with at(-100):
        company = create_company("Old row")
    with at(0):
        poll_table(tracked)
    with at(10):
        poll_table(tracked)
    with at(5):  # updated at t=5, committed after the t=10 poll
        company.company_name = "Updated late"
        company.save()
    with at(20):
        assert poll_table(tracked) == 1
    with at(30):
        assert poll_table(tracked) == 0
    assert len(entries()) == 1


def test_limitation_commit_later_than_the_overlap_is_missed():
    tracked = create_tracked_table(Company)
    with at(0):
        poll_table(tracked)
    with at(100):
        poll_table(tracked)  # cursor: t=40
    with at(39):  # committed after the t=100 poll, 61 s after its created_at
        create_company("Committed too late")
    with at(130):
        assert poll_table(tracked) == 0


@NO_OVERLAP
def test_limitation_late_commit_is_missed_without_overlap():
    """POLL_OVERLAP_SECONDS = 0 restores the 1.0 cursor and its race."""
    tracked = create_tracked_table(Company)
    with at(0):
        poll_table(tracked)
    with at(10):
        poll_table(tracked)
    with at(5):
        create_company("Committed late")
    with at(20):
        assert poll_table(tracked) == 0


def test_update_shortly_after_insert_is_reported_with_overlap():
    """
    With a trailing cursor, a row created after the cursor is reported as
    updated once updated_at is more than a second after created_at.
    """
    channel = create_channel()
    insert = create_tracked_table(Company, "insert", channel=channel)
    update = create_tracked_table(Company, "update", channel=channel)
    with at(0):
        poll_all_tables()
    with at(1):
        company = create_company("New")
    with at(2):
        assert poll_all_tables() == 1
    # Re-reading the overlap does not report the creation as an update.
    with at(3):
        assert poll_all_tables() == 0
    with at(4):
        company.save()
    with at(5):
        assert poll_all_tables() == 1
    with at(6):
        assert poll_all_tables() == 0
    assert len(entries(insert)) == len(entries(update)) == 1


def test_limitation_update_within_a_second_of_creation_collapses():
    channel = create_channel()
    insert = create_tracked_table(Company, "insert", channel=channel)
    update = create_tracked_table(Company, "update", channel=channel)
    with at(0):
        poll_all_tables()
    with at(1):
        company = create_company("New")
    with at(1.5):
        assert poll_all_tables() == 1
    with at(1.9):
        company.save()
    with at(10):
        assert poll_all_tables() == 0
    assert len(entries(insert)) == 1
    assert entries(update) == []


def test_dedup_window_per_tracked_table():
    tracked = create_tracked_table(Company, "update", dedup_window_seconds=0)
    with at(-10):
        company = create_company("Per table")
    with at(0):
        poll_table(tracked)

    _updates_polled_at(tracked, company, [30, 90])

    # The table's own window (0) overrides the 300 s default.
    assert len(entries()) == 2


def test_render_error_does_not_stop_other_rows():
    tracked = create_tracked_table(Company, message_template="{company_name}")
    with at(0):
        poll_table(tracked)
    with at(1):
        create_company("One")
        create_company("Two")

    def flaky(template, instance):
        if instance.company_name == "One":
            raise RuntimeError("boom")
        return render_template(template, instance)

    with (
        at(2),
        patch("webhook_outbox.services.poller.render_template", side_effect=flaky),
    ):
        assert poll_table(tracked) == 1
    assert [e.message for e in entries()] == ["Two"]
    assert reload(tracked).last_polled_at == T0  # retried by the next poll


# --- models: managers and timestamps ------------------------------------------


def test_default_manager_filters_rows_soft_delete():
    tracked = create_tracked_table(SoftDeletedItem)
    with at(0):
        poll_table(tracked)
    with at(1):
        SoftDeletedItem.objects.create(name="visible")
        SoftDeletedItem.objects.create(name="hidden", deleted_at=T0)
    with at(2):
        assert poll_table(tracked) == 1


@NO_OVERLAP
def test_model_without_objects_manager_fails_in_isolation():
    channel = create_channel()
    broken = create_tracked_table(CustomManagerItem, channel=channel)
    healthy = create_tracked_table(Company, channel=channel)
    with at(0):
        poll_all_tables()
    with at(1):
        CustomManagerItem.items.create(name="x")
        create_company("Still delivered")
    with at(2):
        assert poll_all_tables() == 1

    assert len(entries(healthy)) == 1
    # The failing table's cursor is not advanced: the query raised first.
    assert reload(broken).last_polled_at == T0
    assert reload(healthy).last_polled_at == T0 + timedelta(seconds=2)


def test_model_without_timestamps_is_skipped_and_cursor_untouched():
    tracked = create_tracked_table(PlainItem)
    with at(0):
        assert poll_table(tracked) == 0
    assert reload(tracked).last_polled_at is None


def test_insert_only_model():
    channel = create_channel()
    insert = create_tracked_table(InsertOnlyItem, "insert", channel=channel)
    update = create_tracked_table(InsertOnlyItem, "update", channel=channel)
    with at(0):
        poll_all_tables()
    with at(1):
        InsertOnlyItem.objects.create(name="x")
    with at(2):
        assert poll_all_tables() == 1
    assert len(entries(insert)) == 1
    assert reload(update).last_polled_at is None


def test_outbox_entry_fields():
    tracked = create_tracked_table(
        RegistrationRequest,
        message_template="{company.company_name} / {company.company_type.name} / {user.email}",
    )
    with at(0):
        poll_table(tracked)
    with at(1):
        request = create_request()
    with at(2):
        poll_table(tracked)

    (entry,) = entries()
    assert entry.tracked_table == tracked
    assert entry.source_model == "testapp.registrationrequest"
    assert entry.source_pk == str(request.pk)
    assert entry.event_type == "insert"
    assert entry.message == "Test Company / LLC / test@example.com"
    assert entry.status == NotificationOutbox.Status.PENDING
    assert entry.retry_count == 0
    assert entry.created_at == T0 + timedelta(seconds=2)


def test_inactive_tables_and_channels_are_not_polled():
    inactive_table = create_tracked_table(Company, is_active=False)
    inactive_channel = create_tracked_table(
        Company, channel=create_channel(is_active=False)
    )
    with at(0):
        poll_all_tables()
    assert reload(inactive_table).last_polled_at is None
    assert reload(inactive_channel).last_polled_at is None
