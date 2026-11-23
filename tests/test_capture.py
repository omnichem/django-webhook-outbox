"""Optional signal capture, WEBHOOK_OUTBOX["SIGNAL_CAPTURE"] (#4)."""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

import pytest
import time_machine
from django.core import checks
from django.db import transaction
from django.test import override_settings

from tests.factories import (
    create_channel,
    create_company,
    create_organization_type,
    create_tracked_table,
)
from tests.testapp.models import Company, PlainItem
from webhook_outbox import capture
from webhook_outbox.models import NotificationOutbox
from webhook_outbox.services.poller import poll_all_tables

pytestmark = pytest.mark.django_db

V2_CONFIG = {"url": "https://example.com/hook", "envelope_version": 2}
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt_timezone.utc)

CAPTURE_COMPANY = override_settings(
    WEBHOOK_OUTBOX={"SIGNAL_CAPTURE": ["testapp.company"]}
)


def at(seconds: float):
    return time_machine.travel(T0 + timedelta(seconds=seconds), tick=False)


def events():
    return list(
        NotificationOutbox.objects.order_by("pk").values_list("event_type", "source_pk")
    )


@pytest.fixture
def rules():
    channel = create_channel()
    return {
        event_type: create_tracked_table(
            Company,
            event_type,
            channel=channel,
            message_template=f"{event_type} {{company_name}}",
        )
        for event_type in ("insert", "update", "delete")
    }


def test_off_by_default(rules):
    assert capture._connected == {}
    company = create_company()
    company.save()
    company.delete()
    assert events() == []


@CAPTURE_COMPANY
def test_create_update_delete_each_produce_one_event(rules):
    company = create_company("Captured")
    pk = str(company.pk)
    assert events() == [("insert", pk)]

    company.company_name = "Renamed"
    company.save()
    assert events() == [("insert", pk), ("update", pk)]

    company.delete()
    assert events() == [("insert", pk), ("update", pk), ("delete", pk)]

    insert, update, delete = NotificationOutbox.objects.order_by("pk")
    assert insert.message == "insert Captured"
    assert update.message == "update Renamed"
    assert delete.message == "delete Renamed"
    assert delete.source_changed_at is None
    assert {e.tracked_table for e in (insert, update, delete)} == set(rules.values())
    # Polling is not involved: changes are null without "fields".
    assert insert.changes is update.changes is delete.changes is None


@CAPTURE_COMPANY
def test_records_the_row_timestamps(rules):
    company = create_company()
    company.save()
    insert, update = NotificationOutbox.objects.order_by("pk")
    company.refresh_from_db()
    assert insert.source_changed_at == company.created_at
    assert update.source_changed_at == company.updated_at


@CAPTURE_COMPANY
def test_save_with_update_fields_without_updated_at_records_no_timestamp(rules):
    company = create_company()
    company.company_name = "x"
    company.save(update_fields=["company_name"])
    update = NotificationOutbox.objects.get(event_type="update")
    assert update.source_changed_at is None


@CAPTURE_COMPANY
def test_rollback_drops_the_events(rules):
    with pytest.raises(RuntimeError), transaction.atomic():
        create_company()
        raise RuntimeError
    assert events() == []


@CAPTURE_COMPANY
def test_fans_out_to_every_active_rule():
    a = create_tracked_table(Company, "insert")
    b = create_tracked_table(Company, "insert")
    create_tracked_table(Company, "insert", is_active=False)
    create_tracked_table(Company, "insert", channel=create_channel(is_active=False))

    create_company()

    assert set(NotificationOutbox.objects.values_list("tracked_table", flat=True)) == {
        a.pk,
        b.pk,
    }


@override_settings(
    WEBHOOK_OUTBOX={
        "SIGNAL_CAPTURE": ["testapp.company"],
        "DEDUP_WINDOW_SECONDS": 0,
    }
)
def test_polling_the_same_rules_does_not_duplicate(rules):
    with at(-10):
        old = create_company("Old")
    with at(0):
        poll_all_tables()
    with at(1):
        new = create_company("New")
    with at(2):
        old.save()
    for second in (3, 40, 100):
        with at(second):
            assert poll_all_tables() == 0

    assert sorted(events()) == sorted(
        [("insert", str(old.pk)), ("insert", str(new.pk)), ("update", str(old.pk))]
    )


@CAPTURE_COMPANY
def test_limitation_queryset_update_is_not_captured(rules):
    company = create_company()
    Company.objects.filter(pk=company.pk).update(company_name="bulk")
    assert events() == [("insert", str(company.pk))]


@CAPTURE_COMPANY
def test_queryset_delete_is_captured(rules):
    company = create_company()
    Company.objects.filter(pk=company.pk).delete()
    assert events() == [("insert", str(company.pk)), ("delete", str(company.pk))]


@CAPTURE_COMPANY
def test_failure_is_logged_and_does_not_break_the_save(rules, caplog):
    with patch(
        "webhook_outbox.services.enqueue.NotificationOutbox.objects.create",
        side_effect=RuntimeError("outbox down"),
    ):
        company = create_company("Still saved")

    assert Company.objects.filter(pk=company.pk).exists()
    assert events() == []
    assert "Signal capture: cannot queue 'insert'" in caplog.text


@override_settings(
    WEBHOOK_OUTBOX={"SIGNAL_CAPTURE": {"testapp.company": {"fields": ["address"]}}}
)
def test_failure_comparing_values_does_not_break_the_save(rules, caplog):
    company = create_company("Before")

    with patch("webhook_outbox.capture._changes", side_effect=ValueError("odd type")):
        company.company_name = "After"
        company.save()

    company.refresh_from_db()
    assert company.company_name == "After"
    assert ("update", str(company.pk)) in events()
    assert "Signal capture: cannot compare values" in caplog.text


# --- changes ----------------------------------------------------------------------


@override_settings(
    WEBHOOK_OUTBOX={
        "SIGNAL_CAPTURE": {
            "testapp.company": {"fields": ["company_name", "company_type", "address"]}
        }
    }
)
def test_fields_give_old_and_new_values_on_v2_channels():
    v2 = create_tracked_table(
        Company, "update", channel=create_channel(config=V2_CONFIG)
    )
    v1 = create_tracked_table(Company, "update")
    company = create_company("Before", address="Same")
    old_type = company.company_type
    new_type = create_organization_type("JSC")

    company.company_name = "After"
    company.company_type = new_type
    company.save()

    assert NotificationOutbox.objects.get(tracked_table=v2).changes == {
        "company_name": {"old": "Before", "new": "After"},
        "company_type": {"old": old_type.pk, "new": new_type.pk},
    }
    assert NotificationOutbox.objects.get(tracked_table=v1).changes is None


@override_settings(
    WEBHOOK_OUTBOX={"SIGNAL_CAPTURE": {"testapp.company": {"fields": ["address"]}}}
)
def test_no_tracked_field_changed_gives_empty_changes():
    tracked = create_tracked_table(
        Company, "update", channel=create_channel(config=V2_CONFIG)
    )
    company = create_company()
    company.company_name = "other"
    company.save()
    assert NotificationOutbox.objects.get(tracked_table=tracked).changes == {}


def _company_selects(queries):
    table = Company._meta.db_table
    return [
        q["sql"]
        for q in queries
        if q["sql"].startswith("SELECT") and f'FROM "{table}"' in q["sql"]
    ]


@CAPTURE_COMPANY
def test_without_fields_old_values_are_not_read(rules):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    company = create_company()
    with CaptureQueriesContext(connection) as captured:
        company.save()
    assert _company_selects(captured.captured_queries) == []


@override_settings(
    WEBHOOK_OUTBOX={"SIGNAL_CAPTURE": {"testapp.company": {"fields": ["address"]}}}
)
def test_with_fields_old_values_are_read_once(rules):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    company = create_company()
    with CaptureQueriesContext(connection) as captured:
        company.save()
    assert len(_company_selects(captured.captured_queries)) == 1


# --- configuration ----------------------------------------------------------------


def test_setting_changes_reconnect():
    assert capture._connected == {}
    with CAPTURE_COMPANY:
        assert list(capture._connected) == [Company]
    assert capture._connected == {}


def _ids():
    return sorted(
        m.id for m in checks.run_checks() if m.id and m.id.startswith("webhook_outbox.")
    )


@pytest.mark.parametrize(
    "value",
    [
        "testapp.company",
        {"testapp.nope": {}},
        {"nope": {}},
        {"webhook_outbox.notificationoutbox": {}},
        {"testapp.company": {"fields": ["nope"]}},
        {"testapp.company": {"fields": "company_name"}},
        {"testapp.company": {"unknown": 1}},
        {"testapp.company": []},
    ],
)
def test_invalid_capture_settings(value):
    with override_settings(WEBHOOK_OUTBOX={"SIGNAL_CAPTURE": value}):
        assert _ids() == ["webhook_outbox.E008"]


@pytest.mark.parametrize(
    "value",
    [
        [],
        {},
        ["testapp.company", "testapp.plainitem"],
        {"testapp.company": None, "testapp.plainitem": {"fields": ["name"]}},
    ],
)
def test_valid_capture_settings(value):
    with override_settings(WEBHOOK_OUTBOX={"SIGNAL_CAPTURE": value}):
        assert _ids() == []


@override_settings(
    WEBHOOK_OUTBOX={"SIGNAL_CAPTURE": ["testapp.nope", "testapp.plainitem"]}
)
def test_invalid_entries_are_skipped_valid_ones_connected():
    assert list(capture._connected) == [PlainItem]
