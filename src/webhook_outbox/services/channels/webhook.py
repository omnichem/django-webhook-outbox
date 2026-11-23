import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from webhook_outbox.conf import get_setting

from .base import BaseChannel, NotificationPayload

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("http", "https")

# Response bodies land in NotificationOutbox.error_message; cap them so a
# receiver returning an HTML error page doesn't bloat the table.
MAX_ERROR_BODY_CHARS = 500


class WebhookChannel(BaseChannel):
    """
    Generic outgoing webhook channel: POSTs the notification as JSON.

    Required config:
        - url: Absolute http(s) URL to POST to

    Optional config:
        - headers: Flat dict of extra request headers, e.g.
          {"Authorization": "Bearer ..."}. Used for authenticating the call.
        - timeout: Request timeout in seconds (defaults to
          WEBHOOK_OUTBOX["WEBHOOK_TIMEOUT"], then the legacy
          settings.NOTIFICATION_WEBHOOK_TIMEOUT, then 30)

    A response with a 2xx status is treated as success; anything else
    (redirects are not followed, so 3xx too) is reported as a failure so the
    outbox retry counter can pick it up.

    Trust boundary: the URL is configured by staff through the Django admin,
    not by end users. Internal/private addresses are deliberately allowed —
    the expected receiver is a neighbouring service — so validation only
    enforces an absolute http(s) URL and does not deny private IP ranges.

    Auth headers are stored in plain text in NotificationChannel.config
    (see docs/security.md).
    """

    DEFAULT_TIMEOUT_SECONDS = 30

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.url = config["url"]
        self.headers = config.get("headers") or {}
        self.timeout = config.get("timeout") or get_setting("WEBHOOK_TIMEOUT")

    def validate_config(self) -> None:
        """Validate webhook-specific configuration."""
        url = self.config.get("url")
        if not url:
            raise ValueError("Missing required webhook config field: url")

        if not isinstance(url, str):
            raise ValueError("Webhook config field 'url' must be a string")

        parsed = urlparse(url)
        if parsed.scheme not in ALLOWED_SCHEMES or not parsed.netloc:
            raise ValueError(
                f"Invalid webhook url: {url}. "
                "Expected an absolute http:// or https:// URL"
            )

        headers = self.config.get("headers")
        if headers is not None:
            if not isinstance(headers, dict):
                raise ValueError("Webhook config field 'headers' must be an object")
            for key, value in headers.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise ValueError("Webhook headers must be a flat map of strings")

        timeout = self.config.get("timeout")
        if timeout is not None:
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
                raise ValueError("Webhook config field 'timeout' must be a number")
            if timeout <= 0:
                raise ValueError("Webhook config field 'timeout' must be positive")

    def send(self, payload: NotificationPayload) -> tuple[bool, str]:
        """
        POST the notification payload to the configured URL.

        Args:
            payload: The notification to deliver

        Returns:
            Tuple of (success, error_message)
        """
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    self.url,
                    json=payload.as_dict(),
                    headers=self.headers,
                )

                if response.is_success:
                    logger.info(f"Webhook delivered successfully to {self.url}")
                    return True, ""

                error = f"HTTP {response.status_code}: {response.text[:MAX_ERROR_BODY_CHARS]}"
                logger.error(f"Webhook request to {self.url} failed: {error}")
                return False, error

        except httpx.TimeoutException:
            error = "Request timed out"
            logger.error(f"Webhook request to {self.url} timed out")
            return False, error
        except httpx.RequestError as e:
            error = f"Request error: {str(e)}"
            logger.error(f"Webhook request to {self.url} errored: {error}")
            return False, error
        except Exception as e:
            error = f"Unexpected error: {str(e)}"
            logger.exception(f"Webhook unexpected error for {self.url}: {error}")
            return False, error
