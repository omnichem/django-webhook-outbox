"""Ported from the original implementation (logic unchanged; import paths re-pointed)."""

from datetime import datetime
from datetime import timezone as dt_timezone
from unittest.mock import MagicMock, patch

import httpx
from django.test import SimpleTestCase, override_settings

from webhook_outbox.services.channels import BaseChannel, WebhookChannel
from webhook_outbox.services.channels.base import NotificationPayload
from webhook_outbox.services.channels.webhook import MAX_ERROR_BODY_CHARS


def build_payload(**overrides) -> NotificationPayload:
    defaults = {
        "message": "New order A-1042 from Acme Corp",
        "event_type": "insert",
        "source_model": "shop.order",
        "source_pk": "38",
        "tracked_table": "New orders",
        "occurred_at": datetime(2026, 8, 3, 10, 0, tzinfo=dt_timezone.utc),
    }
    defaults.update(overrides)
    return NotificationPayload(**defaults)


class NotificationPayloadTest(SimpleTestCase):
    """Tests for the channel-agnostic payload."""

    def test_as_dict_shape(self):
        """Serialized payload should carry the full event, not just the text."""
        result = build_payload().as_dict()

        self.assertEqual(
            result,
            {
                "event": "insert",
                "source_model": "shop.order",
                "source_pk": "38",
                "tracked_table": "New orders",
                "message": "New order A-1042 from Acme Corp",
                "occurred_at": "2026-08-03T10:00:00+00:00",
            },
        )

    def test_from_outbox(self):
        """Should map outbox fields onto the payload without touching the DB."""
        entry = MagicMock()
        entry.message = "Test message"
        entry.event_type = "update"
        entry.source_model = "shop.order"
        entry.source_pk = "7"
        entry.tracked_table.name = "Status changes"
        entry.created_at = datetime(2026, 8, 3, 12, 30, tzinfo=dt_timezone.utc)

        payload = NotificationPayload.from_outbox(entry)

        self.assertEqual(payload.message, "Test message")
        self.assertEqual(payload.event_type, "update")
        self.assertEqual(payload.source_pk, "7")
        self.assertEqual(payload.tracked_table, "Status changes")
        self.assertEqual(payload.occurred_at, entry.created_at)


class WebhookChannelValidationTest(SimpleTestCase):
    """Tests for webhook config validation."""

    def test_valid_minimal_config(self):
        """Only url is required."""
        channel = WebhookChannel({"url": "https://example.com/hook"})

        self.assertIsInstance(channel, BaseChannel)
        self.assertEqual(channel.url, "https://example.com/hook")
        self.assertEqual(channel.headers, {})

    def test_valid_full_config(self):
        """Headers and timeout should be picked up when present."""
        channel = WebhookChannel(
            {
                "url": "http://internal.svc/hook",
                "headers": {"Authorization": "Bearer secret"},
                "timeout": 5,
            }
        )

        self.assertEqual(channel.headers, {"Authorization": "Bearer secret"})
        self.assertEqual(channel.timeout, 5)

    @override_settings(NOTIFICATION_WEBHOOK_TIMEOUT=17)
    def test_timeout_falls_back_to_setting(self):
        """Without an explicit timeout the global setting applies."""
        channel = WebhookChannel({"url": "https://example.com/hook"})

        self.assertEqual(channel.timeout, 17)

    def test_missing_url(self):
        with self.assertRaises(ValueError) as ctx:
            WebhookChannel({})

        self.assertIn("url", str(ctx.exception))

    def test_empty_url(self):
        with self.assertRaises(ValueError) as ctx:
            WebhookChannel({"url": ""})

        self.assertIn("url", str(ctx.exception))

    def test_rejects_non_http_scheme(self):
        """Non-http(s) schemes must not be dialed."""
        with self.assertRaises(ValueError) as ctx:
            WebhookChannel({"url": "file:///etc/passwd"})

        self.assertIn("Invalid webhook url", str(ctx.exception))

    def test_rejects_relative_url(self):
        with self.assertRaises(ValueError) as ctx:
            WebhookChannel({"url": "/hooks/outbox"})

        self.assertIn("Invalid webhook url", str(ctx.exception))

    def test_rejects_non_dict_headers(self):
        with self.assertRaises(ValueError) as ctx:
            WebhookChannel({"url": "https://example.com", "headers": "Bearer x"})

        self.assertIn("headers", str(ctx.exception))

    def test_rejects_nested_headers(self):
        with self.assertRaises(ValueError) as ctx:
            WebhookChannel(
                {"url": "https://example.com", "headers": {"X-Meta": {"a": 1}}}
            )

        self.assertIn("headers", str(ctx.exception))

    def test_rejects_non_numeric_timeout(self):
        with self.assertRaises(ValueError) as ctx:
            WebhookChannel({"url": "https://example.com", "timeout": "fast"})

        self.assertIn("timeout", str(ctx.exception))

    def test_rejects_non_positive_timeout(self):
        with self.assertRaises(ValueError) as ctx:
            WebhookChannel({"url": "https://example.com", "timeout": 0})

        self.assertIn("timeout", str(ctx.exception))


class WebhookChannelSendTest(SimpleTestCase):
    """Tests for webhook delivery."""

    def setUp(self):
        self.config = {
            "url": "https://example.com/hook",
            "headers": {"Authorization": "Bearer secret"},
        }

    @staticmethod
    def _mock_client(mock_client_class, response):
        mock_client = MagicMock()
        mock_client.post.return_value = response
        mock_client_class.return_value.__enter__.return_value = mock_client
        return mock_client

    @patch("webhook_outbox.services.channels.webhook.httpx.Client")
    def test_send_success(self, mock_client_class):
        """2xx should return (True, '') and POST the structured payload."""
        response = MagicMock()
        response.is_success = True
        mock_client = self._mock_client(mock_client_class, response)

        payload = build_payload()
        success, error = WebhookChannel(self.config).send(payload)

        self.assertTrue(success)
        self.assertEqual(error, "")

        args, kwargs = mock_client.post.call_args
        self.assertEqual(args[0], self.config["url"])
        self.assertEqual(kwargs["json"], payload.as_dict())
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer secret"})

    @patch("webhook_outbox.services.channels.webhook.httpx.Client")
    def test_send_http_error(self, mock_client_class):
        """Non-2xx should return (False, error) so the outbox can retry."""
        response = MagicMock()
        response.is_success = False
        response.status_code = 500
        response.text = "Internal Server Error"
        self._mock_client(mock_client_class, response)

        success, error = WebhookChannel(self.config).send(build_payload())

        self.assertFalse(success)
        self.assertIn("500", error)
        self.assertIn("Internal Server Error", error)

    @patch("webhook_outbox.services.channels.webhook.httpx.Client")
    def test_send_http_error_truncates_body(self, mock_client_class):
        """A huge error page must not be stored in full in error_message."""
        response = MagicMock()
        response.is_success = False
        response.status_code = 502
        response.text = "x" * 5000
        self._mock_client(mock_client_class, response)

        success, error = WebhookChannel(self.config).send(build_payload())

        self.assertFalse(success)
        self.assertIn("502", error)
        self.assertEqual(error.count("x"), MAX_ERROR_BODY_CHARS)

    @patch("webhook_outbox.services.channels.webhook.httpx.Client")
    def test_send_timeout(self, mock_client_class):
        mock_client = MagicMock()
        mock_client.post.side_effect = httpx.TimeoutException("timeout")
        mock_client_class.return_value.__enter__.return_value = mock_client

        success, error = WebhookChannel(self.config).send(build_payload())

        self.assertFalse(success)
        self.assertIn("timed out", error)

    @patch("webhook_outbox.services.channels.webhook.httpx.Client")
    def test_send_connection_error(self, mock_client_class):
        mock_client = MagicMock()
        mock_client.post.side_effect = httpx.ConnectError("connection refused")
        mock_client_class.return_value.__enter__.return_value = mock_client

        success, error = WebhookChannel(self.config).send(build_payload())

        self.assertFalse(success)
        self.assertIn("Request error", error)

    @patch("webhook_outbox.services.channels.webhook.httpx.Client")
    def test_send_unexpected_error(self, mock_client_class):
        mock_client = MagicMock()
        mock_client.post.side_effect = RuntimeError("boom")
        mock_client_class.return_value.__enter__.return_value = mock_client

        success, error = WebhookChannel(self.config).send(build_payload())

        self.assertFalse(success)
        self.assertIn("Unexpected error", error)

    @patch("webhook_outbox.services.channels.webhook.httpx.Client")
    def test_send_without_headers(self, mock_client_class):
        """Missing headers should send an empty header map, not None."""
        response = MagicMock()
        response.is_success = True
        mock_client = self._mock_client(mock_client_class, response)

        WebhookChannel({"url": "https://example.com/hook"}).send(build_payload())

        self.assertEqual(mock_client.post.call_args[1]["headers"], {})
