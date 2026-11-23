# Channels

A channel is a `NotificationChannel` row: a `channel_type` and a JSON
`config`. The type maps to a channel class through the channel registry.

## Webhook (`"webhook"`)

```json
{
  "url": "https://receiver.example.com/hooks/orders",
  "headers": {"Authorization": "Bearer <token>", "X-Source": "shop"},
  "timeout": 10
}
```

| Key | Required | Rules |
| --- | --- | --- |
| `url` | yes | Absolute `http://` or `https://` URL. Other schemes and relative URLs are rejected. |
| `headers` | no | A flat object whose keys and values are all strings. Sent with every request. |
| `timeout` | no | A positive number of seconds. Default: `WEBHOOK_TIMEOUT` (30). |
| `envelope_version` | no | `1` or `2` (1.1+). Default: `ENVELOPE_VERSION` (1). See [envelope.md](envelope.md). |

The request is an HTTP `POST` with the [envelope](envelope.md) as JSON,
made with `httpx.Client(timeout=...)`. Redirects are not followed and TLS
certificates are verified (httpx defaults). Proxy environment variables
(`HTTPS_PROXY`, …) are honoured, as httpx does by default.

An invalid config does not raise. Each delivery attempt fails with the
validation message (for example `Missing required webhook config field: url`)
and uses up one retry.

## The registry

```python
WEBHOOK_OUTBOX = {
    "CHANNELS": {
        "slack": "myproject.notify.SlackChannel",
        # "webhook": None,  # would remove the built-in type
    },
}
```

`CHANNELS` is merged over `{"webhook": "webhook_outbox.services.channels.webhook.WebhookChannel"}`.
The admin offers every registered type. The model field's `choices` only
list `webhook`, so that migrations do not depend on settings; the admin
form validates the type against the registry instead.

The system checks report an entry that cannot be imported or is not a
`BaseChannel` subclass (E003), and channel rows whose type is not
registered (W010, database check). At delivery time an unregistered type
fails with `Unsupported channel type: <type>`.

## Writing a channel

```python
import httpx

from webhook_outbox.services.channels import BaseChannel, NotificationPayload


class SlackChannel(BaseChannel):
    verbose_name = "Slack"  # label in the admin (optional)

    def validate_config(self) -> None:
        # Called from __init__; raise ValueError with a readable message.
        if not str(self.config.get("webhook_url", "")).startswith("https://"):
            raise ValueError("Slack config field 'webhook_url' must be an https URL")

    def send(self, payload: NotificationPayload) -> tuple[bool, str]:
        # Return (True, "") on success or (False, "<error for the admin>").
        try:
            response = httpx.post(
                self.config["webhook_url"], json={"text": payload.message}, timeout=10
            )
        except httpx.HTTPError as e:
            return False, f"Request error: {e}"
        if response.is_success:
            return True, ""
        return False, f"HTTP {response.status_code}: {response.text[:500]}"
```

`NotificationPayload` is a frozen dataclass with `message`, `event_type`,
`source_model`, `source_pk`, `tracked_table` and `occurred_at` (a datetime).
Since 1.1 it also has `event_id`, `changes`, `extra` and `source_changed_at`
(all optional). `payload.as_dict()` is the envelope of
`WEBHOOK_OUTBOX["ENVELOPE_VERSION"]` (v1 by default). `as_dict(2)`,
`as_v1_dict()` and `as_v2_dict()` pick one explicitly, and
`get_envelope_version(channel)` reads a channel's `"envelope_version"` the
way `WebhookChannel` does.

How the dispatcher (`webhook_outbox.services.channels.dispatch`) treats a
channel:

- An inactive channel fails with `Channel is inactive` and the class is
  never instantiated.
- A `ValueError` from `__init__` / `validate_config` / `send` becomes the
  error message.
- Any other exception is logged with its traceback and its `str()` becomes
  the error message.
- The return value of `send` is used as is.
