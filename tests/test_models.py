"""Ported from the original implementation, re-pointed to the test models."""

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError
from django.test import TestCase

from tests.testapp.models import RegistrationRequest
from webhook_outbox.models import NotificationChannel, NotificationOutbox, TrackedTable


class NotificationChannelModelTest(TestCase):
    def test_create_webhook_channel(self):
        channel = NotificationChannel.objects.create(
            name="Test Channel",
            channel_type=NotificationChannel.ChannelType.WEBHOOK,
            config={"url": "https://example.com/hook"},
        )

        self.assertEqual(channel.name, "Test Channel")
        self.assertEqual(channel.channel_type, "webhook")
        self.assertTrue(channel.is_active)
        self.assertIsNotNone(channel.created_at)

    def test_str_representation(self):
        channel = NotificationChannel.objects.create(
            name="Alerts",
            channel_type=NotificationChannel.ChannelType.WEBHOOK,
            config={"url": "https://example.com/hook"},
        )

        self.assertEqual(str(channel), "Alerts (Webhook)")


class TrackedTableModelTest(TestCase):
    def setUp(self):
        self.channel = NotificationChannel.objects.create(
            name="Test Channel",
            channel_type=NotificationChannel.ChannelType.WEBHOOK,
            config={"url": "https://example.com/hook"},
        )
        self.request_ct = ContentType.objects.get_for_model(RegistrationRequest)

    def _create(self, **fields):
        values = {
            "name": "Test",
            "content_type": self.request_ct,
            "event_type": TrackedTable.EventType.INSERT,
            "message_template": "Test",
            "channel": self.channel,
        }
        values.update(fields)
        return TrackedTable.objects.create(**values)

    def test_create_tracked_table_insert(self):
        tracked = self._create(
            name="New Registrations",
            message_template="New registration for {company.company_name}",
        )

        self.assertEqual(tracked.event_type, "insert")
        self.assertIsNone(tracked.last_polled_at)
        self.assertTrue(tracked.is_active)

    def test_create_tracked_table_update(self):
        tracked = self._create(
            name="Status Changes",
            event_type=TrackedTable.EventType.UPDATE,
            message_template="Status changed",
        )

        self.assertEqual(tracked.event_type, "update")

    def test_model_path_property(self):
        tracked = self._create()

        self.assertEqual(tracked.model_path, "testapp.registrationrequest")

    def test_app_label_property(self):
        tracked = self._create()

        self.assertEqual(tracked.app_label, "testapp")

    def test_model_name_property(self):
        tracked = self._create()

        self.assertEqual(tracked.model_name, "registrationrequest")

    def test_get_model_class_valid(self):
        tracked = self._create()

        model_class = tracked.get_model_class()
        self.assertIsNotNone(model_class)
        self.assertEqual(model_class.__name__, "RegistrationRequest")

    def test_unique_constraint(self):
        self._create(name="First")

        with self.assertRaises(IntegrityError):
            self._create(name="Duplicate", message_template="Test 2")

    def test_str_representation(self):
        tracked = self._create(name="Test Rule")

        result = str(tracked)
        self.assertIn("Test Rule", result)


class NotificationOutboxModelTest(TestCase):
    def setUp(self):
        self.channel = NotificationChannel.objects.create(
            name="Test Channel",
            channel_type=NotificationChannel.ChannelType.WEBHOOK,
            config={"url": "https://example.com/hook"},
        )
        self.request_ct = ContentType.objects.get_for_model(RegistrationRequest)
        self.tracked_table = TrackedTable.objects.create(
            name="Test",
            content_type=self.request_ct,
            event_type=TrackedTable.EventType.INSERT,
            message_template="Test",
            channel=self.channel,
        )

    def test_create_outbox_entry(self):
        entry = NotificationOutbox.objects.create(
            tracked_table=self.tracked_table,
            source_model="testapp.registrationrequest",
            source_pk="123",
            event_type="insert",
            message="Test notification message",
        )

        self.assertEqual(entry.status, NotificationOutbox.Status.PENDING)
        self.assertEqual(entry.retry_count, 0)
        self.assertIsNone(entry.sent_at)
        self.assertEqual(entry.error_message, "")

    def test_status_choices(self):
        entry = NotificationOutbox.objects.create(
            tracked_table=self.tracked_table,
            source_model="test.Model",
            source_pk="1",
            event_type="insert",
            message="Test",
        )

        self.assertEqual(entry.status, "pending")

        entry.status = NotificationOutbox.Status.SENT
        entry.save()
        entry.refresh_from_db()
        self.assertEqual(entry.status, "sent")

        entry.status = NotificationOutbox.Status.FAILED
        entry.save()
        entry.refresh_from_db()
        self.assertEqual(entry.status, "failed")

    def test_str_representation(self):
        entry = NotificationOutbox.objects.create(
            tracked_table=self.tracked_table,
            source_model="testapp.registrationrequest",
            source_pk="42",
            event_type="insert",
            message="Test",
        )

        result = str(entry)
        self.assertIn("testapp.registrationrequest", result)
        self.assertIn("42", result)
        self.assertIn("pending", result)


class SchemaTest(TestCase):
    """The schema the 1.0 migration ships (equal to the original final schema)."""

    def test_unique_together(self):
        self.assertEqual(
            TrackedTable._meta.unique_together,
            (("content_type", "event_type", "channel"),),
        )

    def test_outbox_index(self):
        self.assertEqual(
            [index.fields for index in NotificationOutbox._meta.indexes],
            [["status", "created_at"]],
        )
        self.assertTrue(NotificationOutbox._meta.get_field("status").db_index)

    def test_timestamp_fields(self):
        for model in (NotificationChannel, TrackedTable, NotificationOutbox):
            created = model._meta.get_field("created_at")
            updated = model._meta.get_field("updated_at")
            self.assertTrue(created.auto_now_add)
            self.assertTrue(updated.auto_now)
            self.assertEqual(str(created.verbose_name), "Created at")
            self.assertEqual(str(updated.verbose_name), "Updated at")

    def test_field_names(self):
        def names(model):
            return [f.name for f in model._meta.concrete_fields]

        self.assertEqual(
            names(NotificationChannel),
            [
                "id",
                "created_at",
                "updated_at",
                "name",
                "channel_type",
                "config",
                "is_active",
            ],
        )
        self.assertEqual(
            names(TrackedTable),
            [
                "id",
                "created_at",
                "updated_at",
                "name",
                "content_type",
                "event_type",
                "message_template",
                "channel",
                "last_polled_at",
                "is_active",
            ],
        )
        self.assertEqual(
            names(NotificationOutbox),
            [
                "id",
                "created_at",
                "updated_at",
                "tracked_table",
                "source_model",
                "source_pk",
                "event_type",
                "message",
                "status",
                "error_message",
                "sent_at",
                "retry_count",
            ],
        )
