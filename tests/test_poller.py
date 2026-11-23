"""Ported from the original implementation, re-pointed to the test models."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone

from tests.factories import create_company, create_person
from tests.testapp.models import Person, RegistrationRequest
from webhook_outbox.models import NotificationChannel, NotificationOutbox, TrackedTable
from webhook_outbox.services.poller import (
    DetectedChange,
    TablePoller,
    _is_duplicate,
    poll_all_tables,
    poll_table,
)


class TablePollerTest(TestCase):
    def setUp(self):
        self.channel = NotificationChannel.objects.create(
            name="Test Channel",
            channel_type=NotificationChannel.ChannelType.WEBHOOK,
            config={"url": "https://example.com/hook"},
        )
        self.user = create_person("test@example.com")
        self.company = create_company("Test Company", "1234567890")
        self.request_ct = ContentType.objects.get_for_model(RegistrationRequest)
        self.user_ct = ContentType.objects.get_for_model(Person)

    def test_first_poll_initializes_timestamp(self):
        """First poll should set last_polled_at and return no changes."""
        tracked = TrackedTable.objects.create(
            name="Test",
            content_type=self.request_ct,
            event_type=TrackedTable.EventType.INSERT,
            message_template="Test: {company.company_name}",
            channel=self.channel,
        )

        self.assertIsNone(tracked.last_polled_at)

        poller = TablePoller(tracked)
        changes = poller.poll()

        tracked.refresh_from_db()
        self.assertEqual(len(changes), 0)
        self.assertIsNotNone(tracked.last_polled_at)

    def test_detect_inserts(self):
        """Should detect records created after last_polled_at."""
        past = timezone.now() - timedelta(minutes=5)

        tracked = TrackedTable.objects.create(
            name="Test",
            content_type=self.request_ct,
            event_type=TrackedTable.EventType.INSERT,
            message_template="Test: {company.company_name}",
            channel=self.channel,
            last_polled_at=past,
        )

        RegistrationRequest.objects.create(company=self.company, user=self.user)

        poller = TablePoller(tracked)
        changes = poller.poll()

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].event_type, "insert")

    def test_detect_inserts_ignores_old_records(self):
        """Should not detect records created before last_polled_at."""
        RegistrationRequest.objects.create(company=self.company, user=self.user)

        future = timezone.now() + timedelta(minutes=5)
        tracked = TrackedTable.objects.create(
            name="Test",
            content_type=self.request_ct,
            event_type=TrackedTable.EventType.INSERT,
            message_template="Test",
            channel=self.channel,
            last_polled_at=future,
        )

        poller = TablePoller(tracked)
        changes = poller.poll()

        self.assertEqual(len(changes), 0)

    def test_detect_updates(self):
        """Should detect records updated after last_polled_at."""
        request = RegistrationRequest.objects.create(
            company=self.company, user=self.user
        )

        poll_time = timezone.now() + timedelta(seconds=1)

        tracked = TrackedTable.objects.create(
            name="Test",
            content_type=self.request_ct,
            event_type=TrackedTable.EventType.UPDATE,
            message_template="Updated: {company.company_name}",
            channel=self.channel,
            last_polled_at=poll_time,
        )

        request.processed = True
        request.save()

        RegistrationRequest.objects.filter(pk=request.pk).update(
            updated_at=poll_time + timedelta(seconds=1)
        )

        poller = TablePoller(tracked)
        changes = poller.poll()

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].event_type, "update")

    def test_detect_updates_ignores_new_records(self):
        """UPDATE detection should ignore newly created records."""
        past = timezone.now() - timedelta(minutes=5)

        tracked = TrackedTable.objects.create(
            name="Test",
            content_type=self.request_ct,
            event_type=TrackedTable.EventType.UPDATE,
            message_template="Test",
            channel=self.channel,
            last_polled_at=past,
        )

        RegistrationRequest.objects.create(company=self.company, user=self.user)

        poller = TablePoller(tracked)
        changes = poller.poll()

        self.assertEqual(len(changes), 0)

    def test_invalid_model_returns_empty(self):
        """Invalid model should return empty changes list."""
        fake_ct = ContentType.objects.create(
            app_label="fake",
            model="fakemodel",
        )

        tracked = TrackedTable.objects.create(
            name="Test",
            content_type=fake_ct,
            event_type=TrackedTable.EventType.INSERT,
            message_template="Test",
            channel=self.channel,
            last_polled_at=timezone.now(),
        )

        poller = TablePoller(tracked)
        changes = poller.poll()

        self.assertEqual(len(changes), 0)


class PollTableTest(TestCase):
    def setUp(self):
        self.channel = NotificationChannel.objects.create(
            name="Test Channel",
            channel_type=NotificationChannel.ChannelType.WEBHOOK,
            config={"url": "https://example.com/hook"},
        )
        self.user = create_person("test@example.com")
        self.company = create_company("Test Company", "1234567890")
        self.request_ct = ContentType.objects.get_for_model(RegistrationRequest)

    def test_poll_creates_outbox_entries(self):
        """Detected changes should create outbox entries."""
        past = timezone.now() - timedelta(minutes=5)

        tracked = TrackedTable.objects.create(
            name="Test",
            content_type=self.request_ct,
            event_type=TrackedTable.EventType.INSERT,
            message_template="New: {company.company_name}",
            channel=self.channel,
            last_polled_at=past,
        )

        request = RegistrationRequest.objects.create(
            company=self.company, user=self.user
        )

        queued = poll_table(tracked)

        self.assertEqual(queued, 1)
        self.assertEqual(NotificationOutbox.objects.count(), 1)

        entry = NotificationOutbox.objects.first()
        self.assertEqual(entry.source_pk, str(request.pk))
        self.assertEqual(entry.status, NotificationOutbox.Status.PENDING)
        self.assertIn("Test Company", entry.message)

    def test_poll_renders_template(self):
        """Outbox message should have rendered template."""
        past = timezone.now() - timedelta(minutes=5)

        tracked = TrackedTable.objects.create(
            name="Test",
            content_type=self.request_ct,
            event_type=TrackedTable.EventType.INSERT,
            message_template="Registration request for {company.company_name} (No. {company.registration_number})",
            channel=self.channel,
            last_polled_at=past,
        )

        RegistrationRequest.objects.create(company=self.company, user=self.user)

        poll_table(tracked)

        entry = NotificationOutbox.objects.first()
        self.assertIn("Test Company", entry.message)
        self.assertIn("1234567890", entry.message)


class DuplicateDetectionTest(TestCase):
    def setUp(self):
        self.channel = NotificationChannel.objects.create(
            name="Test Channel",
            channel_type=NotificationChannel.ChannelType.WEBHOOK,
            config={"url": "https://example.com/hook"},
        )
        self.request_ct = ContentType.objects.get_for_model(RegistrationRequest)
        self.tracked = TrackedTable.objects.create(
            name="Test",
            content_type=self.request_ct,
            event_type=TrackedTable.EventType.INSERT,
            message_template="Test",
            channel=self.channel,
        )
        self.user = create_person("test@example.com")
        self.company = create_company("Test Company", "1234567890")

    def test_is_duplicate_returns_false_for_new(self):
        """New changes should not be detected as duplicates."""
        request = RegistrationRequest.objects.create(
            company=self.company, user=self.user
        )

        change = DetectedChange(
            instance=request,
            event_type=TrackedTable.EventType.INSERT,
        )

        self.assertFalse(_is_duplicate(self.tracked, change))

    def test_is_duplicate_returns_true_for_existing(self):
        """Existing recent outbox entry should be detected as duplicate."""
        request = RegistrationRequest.objects.create(
            company=self.company, user=self.user
        )

        NotificationOutbox.objects.create(
            tracked_table=self.tracked,
            source_model="testapp.registrationrequest",
            source_pk=str(request.pk),
            event_type="insert",
            message="Test",
        )

        change = DetectedChange(
            instance=request,
            event_type=TrackedTable.EventType.INSERT,
        )

        self.assertTrue(_is_duplicate(self.tracked, change))

    def test_duplicate_not_created(self):
        """Polling should skip duplicate notifications."""
        past = timezone.now() - timedelta(minutes=5)
        self.tracked.last_polled_at = past
        self.tracked.save()

        request = RegistrationRequest.objects.create(
            company=self.company, user=self.user
        )

        NotificationOutbox.objects.create(
            tracked_table=self.tracked,
            source_model="testapp.registrationrequest",
            source_pk=str(request.pk),
            event_type="insert",
            message="Existing entry",
        )

        initial_count = NotificationOutbox.objects.count()
        poll_table(self.tracked)

        self.assertEqual(NotificationOutbox.objects.count(), initial_count)


class PollAllTablesTest(TestCase):
    def setUp(self):
        self.channel = NotificationChannel.objects.create(
            name="Test Channel",
            channel_type=NotificationChannel.ChannelType.WEBHOOK,
            config={"url": "https://example.com/hook"},
        )
        self.user_ct = ContentType.objects.get_for_model(Person)

    def test_polls_only_active_tables(self):
        """Should only poll active tracked tables with active channels."""
        past = timezone.now() - timedelta(minutes=5)

        TrackedTable.objects.create(
            name="Active",
            content_type=self.user_ct,
            event_type=TrackedTable.EventType.INSERT,
            message_template="Test",
            channel=self.channel,
            last_polled_at=past,
            is_active=True,
        )

        TrackedTable.objects.create(
            name="Inactive",
            content_type=self.user_ct,
            event_type=TrackedTable.EventType.UPDATE,
            message_template="Test",
            channel=self.channel,
            last_polled_at=past,
            is_active=False,
        )

        total = poll_all_tables()

        self.assertIsInstance(total, int)

    def test_skips_inactive_channels(self):
        """Should skip tables with inactive channels."""
        inactive_channel = NotificationChannel.objects.create(
            name="Inactive Channel",
            channel_type=NotificationChannel.ChannelType.WEBHOOK,
            config={"url": "https://example.com/hook"},
            is_active=False,
        )

        past = timezone.now() - timedelta(minutes=5)

        TrackedTable.objects.create(
            name="Test",
            content_type=self.user_ct,
            event_type=TrackedTable.EventType.INSERT,
            message_template="Test",
            channel=inactive_channel,
            last_polled_at=past,
            is_active=True,
        )

        with patch("webhook_outbox.services.poller.TablePoller.poll") as mock_poll:
            poll_all_tables()
            mock_poll.assert_not_called()
