"""
Ported from the original tests of the Celery tasks: the task bodies are now
plain functions in webhook_outbox.services.delivery / webhook_outbox.runner.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.utils import timezone

from tests.factories import WEBHOOK_CONFIG, create_company, create_person
from tests.testapp.models import RegistrationRequest
from webhook_outbox import runner
from webhook_outbox.models import NotificationChannel, NotificationOutbox, TrackedTable
from webhook_outbox.services.delivery import (
    process_outbox,
    send_notification,
    send_single_notification,
)


def mock_channel(return_value=(True, "")):
    """Build a channel class mock and the instance it returns."""
    instance = MagicMock()
    instance.send.return_value = return_value
    channel_class = MagicMock(return_value=instance)
    return channel_class, instance


def channels_setting(channel_class):
    # MagicMock is not a BaseChannel subclass: register a real subclass that
    # delegates to the mock.
    from webhook_outbox.services.channels import BaseChannel

    class Delegating(BaseChannel):
        def __init__(self, config):
            self._inner = channel_class(config)

        def validate_config(self):
            pass

        def send(self, payload):
            return self._inner.send(payload)

    return {"CHANNELS": {NotificationChannel.ChannelType.WEBHOOK: Delegating}}


class SendNotificationTest(TestCase):
    def setUp(self):
        self.channel = NotificationChannel.objects.create(
            name="Test Channel",
            channel_type=NotificationChannel.ChannelType.WEBHOOK,
            config=WEBHOOK_CONFIG,
        )
        self.request_ct = ContentType.objects.get_for_model(RegistrationRequest)
        self.tracked = TrackedTable.objects.create(
            name="Test",
            content_type=self.request_ct,
            event_type=TrackedTable.EventType.INSERT,
            message_template="Test",
            channel=self.channel,
        )
        self.entry = NotificationOutbox.objects.create(
            tracked_table=self.tracked,
            source_model="testapp.registrationrequest",
            source_pk="1",
            event_type="insert",
            message="Test notification",
        )

    def test_send_notification_webhook(self):
        """Should build a payload from the entry and hand it to the channel."""
        channel_class, instance = mock_channel()

        with override_settings(WEBHOOK_OUTBOX=channels_setting(channel_class)):
            success, error = send_notification(self.entry)

        self.assertTrue(success)
        self.assertEqual(error, "")
        channel_class.assert_called_once_with(WEBHOOK_CONFIG)

        payload = instance.send.call_args[0][0]
        self.assertEqual(payload.message, "Test notification")
        self.assertEqual(payload.event_type, "insert")
        self.assertEqual(payload.source_pk, "1")
        self.assertEqual(payload.tracked_table, "Test")

    def test_send_notification_channel_failure(self):
        """Channel failure should be propagated as (False, error)."""
        channel_class, _ = mock_channel((False, "HTTP 500: boom"))

        with override_settings(WEBHOOK_OUTBOX=channels_setting(channel_class)):
            success, error = send_notification(self.entry)

        self.assertFalse(success)
        self.assertEqual(error, "HTTP 500: boom")

    def test_send_notification_invalid_config(self):
        """Invalid channel config should fail gracefully, not raise."""
        self.channel.config = {}
        self.channel.save()

        success, error = send_notification(self.entry)

        self.assertFalse(success)
        self.assertIn("url", error)

    def test_send_notification_inactive_channel(self):
        """Should return error for inactive channel."""
        self.channel.is_active = False
        self.channel.save()

        success, error = send_notification(self.entry)

        self.assertFalse(success)
        self.assertIn("inactive", error.lower())
        self.assertEqual(error, "Channel is inactive")

    def test_send_notification_unsupported_type(self):
        """Should return error for unsupported channel type."""
        self.channel.channel_type = "unsupported"
        self.channel.save()

        success, error = send_notification(self.entry)

        self.assertFalse(success)
        self.assertIn("Unsupported", error)
        self.assertEqual(error, "Unsupported channel type: unsupported")

    def test_send_notification_channel_raises(self):
        """An unexpected exception from a channel becomes (False, str(exc))."""
        channel_class, instance = mock_channel()
        instance.send.side_effect = RuntimeError("kaboom")

        with override_settings(WEBHOOK_OUTBOX=channels_setting(channel_class)):
            success, error = send_notification(self.entry)

        self.assertEqual((success, error), (False, "kaboom"))


class ProcessOutboxTest(TestCase):
    def setUp(self):
        self.channel = NotificationChannel.objects.create(
            name="Test Channel",
            channel_type=NotificationChannel.ChannelType.WEBHOOK,
            config=WEBHOOK_CONFIG,
        )
        self.request_ct = ContentType.objects.get_for_model(RegistrationRequest)
        self.tracked = TrackedTable.objects.create(
            name="Test",
            content_type=self.request_ct,
            event_type=TrackedTable.EventType.INSERT,
            message_template="Test",
            channel=self.channel,
        )

    def _entry(self, **fields):
        values = {
            "tracked_table": self.tracked,
            "source_model": "test.Model",
            "source_pk": "1",
            "event_type": "insert",
            "message": "Test",
            "status": NotificationOutbox.Status.PENDING,
        }
        values.update(fields)
        return NotificationOutbox.objects.create(**values)

    @patch("webhook_outbox.services.delivery.send_notification")
    def test_process_outbox_sends_pending(self, mock_send):
        """Should process pending notifications."""
        mock_send.return_value = (True, "")

        entry = self._entry()

        result = process_outbox()

        entry.refresh_from_db()
        self.assertEqual(entry.status, NotificationOutbox.Status.SENT)
        self.assertIsNotNone(entry.sent_at)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 0)

    @patch("webhook_outbox.services.delivery.send_notification")
    def test_process_outbox_handles_failure(self, mock_send):
        """Failed send should increment retry count."""
        mock_send.return_value = (False, "Network error")

        entry = self._entry()

        process_outbox()

        entry.refresh_from_db()
        self.assertEqual(entry.status, NotificationOutbox.Status.PENDING)
        self.assertEqual(entry.retry_count, 1)
        self.assertEqual(entry.error_message, "Network error")

    @patch("webhook_outbox.services.delivery.send_notification")
    @override_settings(NOTIFICATION_MAX_RETRIES=2)
    def test_process_outbox_marks_failed_after_retries(self, mock_send):
        """Should mark as failed after max retries."""
        mock_send.return_value = (False, "Persistent error")

        entry = self._entry(retry_count=1)

        result = process_outbox()

        entry.refresh_from_db()
        self.assertEqual(entry.status, NotificationOutbox.Status.FAILED)
        self.assertEqual(entry.retry_count, 2)
        self.assertEqual(result["failed"], 1)

    @patch("webhook_outbox.services.delivery.send_notification")
    def test_process_outbox_skips_sent(self, mock_send):
        """Should skip already sent notifications."""
        self._entry(status=NotificationOutbox.Status.SENT, sent_at=timezone.now())

        process_outbox()

        mock_send.assert_not_called()

    @patch("webhook_outbox.services.delivery.send_notification")
    def test_process_outbox_skips_failed(self, mock_send):
        """Should skip already failed notifications."""
        self._entry(status=NotificationOutbox.Status.FAILED, retry_count=3)

        process_outbox()

        mock_send.assert_not_called()

    # --- added: behaviour pinned for 1.0 ------------------------------------

    @patch("webhook_outbox.services.delivery.send_notification")
    def test_process_outbox_skips_pending_at_max_retries(self, mock_send):
        """A PENDING entry with retry_count >= MAX_RETRIES is never picked up."""
        self._entry(retry_count=3)

        self.assertEqual(process_outbox(), {"sent": 0, "failed": 0})
        mock_send.assert_not_called()

    @patch("webhook_outbox.services.delivery.send_notification")
    def test_success_clears_previous_error(self, mock_send):
        mock_send.return_value = (True, "")
        entry = self._entry(retry_count=2, error_message="HTTP 500: old")

        process_outbox()

        entry.refresh_from_db()
        self.assertEqual(entry.status, NotificationOutbox.Status.SENT)
        self.assertEqual(entry.error_message, "")
        self.assertEqual(entry.retry_count, 2)

    @patch("webhook_outbox.services.delivery.send_notification")
    def test_batch_is_ordered_by_created_at_and_limited(self, mock_send):
        mock_send.return_value = (True, "")
        now = timezone.now()
        entries = [self._entry(source_pk=str(i)) for i in range(5)]
        # oldest first: reverse the creation order
        for offset, entry in enumerate(reversed(entries)):
            NotificationOutbox.objects.filter(pk=entry.pk).update(
                created_at=now - timedelta(minutes=10 - offset)
            )

        result = process_outbox(limit=3)

        self.assertEqual(result, {"sent": 3, "failed": 0})
        sent_pks = [call.args[0].source_pk for call in mock_send.call_args_list]
        self.assertEqual(sent_pks, ["4", "3", "2"])

    @patch("webhook_outbox.services.delivery.send_notification")
    @override_settings(WEBHOOK_OUTBOX={"DELIVERY_BATCH_SIZE": 2})
    def test_batch_size_setting(self, mock_send):
        mock_send.return_value = (True, "")
        for i in range(3):
            self._entry(source_pk=str(i))

        self.assertEqual(process_outbox(), {"sent": 2, "failed": 0})

    @patch("webhook_outbox.services.delivery.send_notification")
    def test_default_batch_size_is_100(self, mock_send):
        mock_send.return_value = (True, "")
        NotificationOutbox.objects.bulk_create(
            [
                NotificationOutbox(
                    tracked_table=self.tracked,
                    source_model="test.Model",
                    source_pk=str(i),
                    event_type="insert",
                    message="Test",
                )
                for i in range(101)
            ]
        )

        self.assertEqual(process_outbox(), {"sent": 100, "failed": 0})

    @patch("webhook_outbox.services.delivery.send_notification")
    def test_exception_while_recording_counts_as_failed(self, mock_send):
        mock_send.side_effect = RuntimeError("boom")
        entry = self._entry()

        self.assertEqual(process_outbox(), {"sent": 0, "failed": 1})
        entry.refresh_from_db()
        self.assertEqual(entry.status, NotificationOutbox.Status.PENDING)
        self.assertEqual(entry.retry_count, 0)


class SendSingleNotificationTest(TestCase):
    def setUp(self):
        self.channel = NotificationChannel.objects.create(
            name="Test Channel",
            channel_type=NotificationChannel.ChannelType.WEBHOOK,
            config=WEBHOOK_CONFIG,
        )
        self.tracked = TrackedTable.objects.create(
            name="Test",
            content_type=ContentType.objects.get_for_model(RegistrationRequest),
            event_type=TrackedTable.EventType.INSERT,
            message_template="Test",
            channel=self.channel,
        )
        self.entry = NotificationOutbox.objects.create(
            tracked_table=self.tracked,
            source_model="test.Model",
            source_pk="1",
            event_type="insert",
            message="Test",
        )

    def test_not_found(self):
        self.assertEqual(send_single_notification(10**9), {"error": "Entry not found"})

    @patch("webhook_outbox.services.delivery.send_notification")
    def test_already_sent(self, mock_send):
        self.entry.status = NotificationOutbox.Status.SENT
        self.entry.save()

        self.assertEqual(
            send_single_notification(self.entry.pk), {"status": "already_sent"}
        )
        mock_send.assert_not_called()

    @patch("webhook_outbox.services.delivery.send_notification")
    def test_success(self, mock_send):
        mock_send.return_value = (True, "")

        result = send_single_notification(self.entry.pk)

        self.assertEqual(result, {"success": True, "error": ""})
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, NotificationOutbox.Status.SENT)
        self.assertIsNotNone(self.entry.sent_at)

    @patch("webhook_outbox.services.delivery.send_notification")
    def test_failure_records_attempt(self, mock_send):
        mock_send.return_value = (False, "HTTP 503: down")

        result = send_single_notification(self.entry.pk)

        self.assertEqual(result, {"success": False, "error": "HTTP 503: down"})
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, NotificationOutbox.Status.PENDING)
        self.assertEqual(self.entry.retry_count, 1)
        self.assertEqual(self.entry.error_message, "HTTP 503: down")

    @patch("webhook_outbox.services.delivery.send_notification")
    def test_failed_entry_is_sent_regardless_of_retry_count(self, mock_send):
        mock_send.return_value = (True, "")
        self.entry.status = NotificationOutbox.Status.FAILED
        self.entry.retry_count = 3
        self.entry.save()

        send_single_notification(self.entry.pk)

        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, NotificationOutbox.Status.SENT)

    @patch("webhook_outbox.services.delivery.send_notification")
    def test_failure_at_max_retries_marks_failed(self, mock_send):
        mock_send.return_value = (False, "nope")
        self.entry.retry_count = 2
        self.entry.save()

        send_single_notification(self.entry.pk)

        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, NotificationOutbox.Status.FAILED)
        self.assertEqual(self.entry.retry_count, 3)


class PollOnceTest(TestCase):
    """Port of the poll task tests: the task body is runner.poll_once()."""

    @patch("webhook_outbox.runner.poll_all_tables")
    def test_poll_once_success(self, mock_poll):
        """Should call poll_all_tables and return result."""
        mock_poll.return_value = 5

        result = runner.poll_once()

        mock_poll.assert_called_once()
        self.assertEqual(result, 5)

    @patch("webhook_outbox.runner.poll_all_tables")
    def test_poll_once_error_propagates(self, mock_poll):
        """Errors propagate to the caller (Celery task retries, worker loop logs)."""
        mock_poll.side_effect = Exception("Database error")

        with self.assertRaises(Exception):  # noqa: B017 - ported as is
            runner.poll_once()


class IntegrationTest(TestCase):
    """End-to-end integration tests."""

    def setUp(self):
        self.channel = NotificationChannel.objects.create(
            name="Test Channel",
            channel_type=NotificationChannel.ChannelType.WEBHOOK,
            config=WEBHOOK_CONFIG,
        )
        self.user = create_person("test@example.com")
        self.company = create_company("Integration Test Corp", "9999999999")
        self.request_ct = ContentType.objects.get_for_model(RegistrationRequest)

    def test_full_flow_insert(self):
        """Test complete flow: create record -> poll -> POST webhook."""
        past = timezone.now() - timedelta(minutes=5)
        TrackedTable.objects.create(
            name="Test Registration Alerts",
            content_type=self.request_ct,
            event_type=TrackedTable.EventType.INSERT,
            message_template="New registration for {company.company_name}",
            channel=self.channel,
            last_polled_at=past,
        )

        RegistrationRequest.objects.create(company=self.company, user=self.user)

        runner.poll_once()

        self.assertEqual(NotificationOutbox.objects.count(), 1)
        entry = NotificationOutbox.objects.first()
        self.assertIn("Integration Test Corp", entry.message)
        self.assertEqual(entry.status, NotificationOutbox.Status.PENDING)

        with patch(
            "webhook_outbox.services.channels.webhook.httpx.Client"
        ) as mock_client_class:
            response = MagicMock()
            response.is_success = True
            mock_client = MagicMock()
            mock_client.post.return_value = response
            mock_client_class.return_value.__enter__.return_value = mock_client

            runner.deliver_once()

        entry.refresh_from_db()
        self.assertEqual(entry.status, NotificationOutbox.Status.SENT)

        mock_client.post.assert_called_once()
        args, kwargs = mock_client.post.call_args
        self.assertEqual(args[0], WEBHOOK_CONFIG["url"])
        self.assertIn("Integration Test Corp", kwargs["json"]["message"])
        self.assertEqual(kwargs["json"]["event"], "insert")
        self.assertEqual(kwargs["json"]["source_model"], "testapp.registrationrequest")
        self.assertEqual(kwargs["json"]["tracked_table"], "Test Registration Alerts")
